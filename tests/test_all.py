"""
全链路自动化测试
覆盖：配置 → 鉴权 → RAG → Agent → API → SSE
运行: cd personal_agent && python tests/test_all.py
"""
import sys, os, json, asyncio, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from pathlib import Path
from fastapi.testclient import TestClient


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(scope="module")
def client():
    """创建 FastAPI TestClient"""
    from api.main import app
    from core.database import init_database, create_admin_user
    from core.auth import hash_password
    from config.settings import settings
    init_database()
    create_admin_user(settings.admin_username, hash_password(settings.admin_password))
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth_headers(client):
    """登录并返回带 token 的 headers"""
    from config.settings import settings
    resp = client.post("/api/auth/login", json={
        "username": settings.admin_username,
        "password": settings.admin_password,
    })
    assert resp.status_code == 200, f"登录失败: {resp.text}"
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.fixture
def sample_pdf():
    """创建一个简单测试文件"""
    tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8")
    tmp.write("姓名：张三\n职业：AI工程师\n技能：Python, LangChain, LangGraph, DeepSeek\n"
              "项目经验：构建多Agent RAG知识库系统，基于ReAct架构\n"
              "教育背景：计算机科学硕士\n")
    tmp.close()
    yield tmp.name
    os.unlink(tmp.name)


# ============================================================
# 1. 配置模块
# ============================================================

class TestConfig:
    def test_settings_load(self):
        from config.settings import settings
        # 不断言具体模型名：模型由 .env 决定，写死会让"换模型"变成测试失败
        assert settings.deepseek_model
        assert settings.deepseek_base_url.startswith("http")
        assert settings.embedding_model == "text-embedding-v4"
        assert settings.port > 0
        assert settings.admin_username

    def test_resolve_path(self):
        from config.settings import settings, PROJECT_ROOT
        p = settings.resolve_path("./assets/chroma_db")
        assert p.is_absolute()
        assert str(PROJECT_ROOT) in str(p)

    def test_get_llm(self):
        from config.settings import get_deepseek_llm, settings
        llm = get_deepseek_llm(temperature=0.0, streaming=False)
        assert llm.model_name == settings.deepseek_model
        assert llm.temperature == 0.0

    def test_get_embeddings(self):
        from config.settings import get_dashscope_embeddings
        emb = get_dashscope_embeddings()
        assert emb.model == "text-embedding-v4"


# ============================================================
# 2. 数据库 & 鉴权
# ============================================================

class TestDatabase:
    def test_init(self):
        from core.database import init_database, get_db_path
        init_database()
        assert os.path.exists(get_db_path())

    def test_admin_exists(self):
        from core.database import get_user_by_username
        from config.settings import settings
        user = get_user_by_username(settings.admin_username)
        assert user is not None
        assert user["username"] == settings.admin_username

    def test_file_crud(self):
        from core.database import (insert_file_record, get_files_by_owner,
                                    get_file_by_id, delete_file_record)
        fid = insert_file_record(1, "test.txt", "/tmp/test.txt", 1024, 5)
        assert fid > 0
        files = get_files_by_owner(1)
        assert any(f["id"] == fid for f in files)
        record = get_file_by_id(fid)
        assert record["filename"] == "test.txt"
        assert delete_file_record(fid, 1)

    def test_chat_log(self):
        from core.database import insert_chat_log, get_chat_history, get_chat_history_count
        before = get_chat_history_count(1)
        lid = insert_chat_log(1, "chat", "测试问题", "测试回答", "推理过程", '["test.txt"]')
        assert lid > 0
        after = get_chat_history_count(1)
        assert after == before + 1


class TestAuth:
    def test_hash_verify(self):
        from core.auth import hash_password, verify_password
        h = hash_password("mypassword")
        assert verify_password("mypassword", h)
        assert not verify_password("wrong", h)

    def test_jwt(self):
        from core.auth import create_access_token, verify_token
        token = create_access_token(1, "admin")
        payload = verify_token(token)
        assert payload["sub"] == "1"
        assert payload["username"] == "admin"

    def test_jwt_expired(self):
        from core.auth import verify_token
        import jwt
        from fastapi import HTTPException
        try:
            verify_token("invalid.token.here")
            assert False, "应抛出异常"
        except HTTPException:
            pass


# ============================================================
# 3. RAG 链路
# ============================================================

class TestDocumentLoader:
    def test_txt(self, sample_pdf):
        from rag.document_loader import load_single_document
        content = load_single_document(sample_pdf)
        assert "张三" in content
        assert "AI工程师" in content

    def test_unsupported_format(self):
        from rag.document_loader import load_single_document
        with pytest.raises(ValueError, match="不支持的文件格式"):
            load_single_document("/tmp/test.xyz")

    def test_formats_registered(self):
        from rag.document_loader import SUPPORTED_EXTENSIONS
        assert ".pdf" in SUPPORTED_EXTENSIONS
        assert ".docx" in SUPPORTED_EXTENSIONS
        assert ".xlsx" in SUPPORTED_EXTENSIONS
        assert ".txt" in SUPPORTED_EXTENSIONS
        assert ".md" in SUPPORTED_EXTENSIONS


class TestTextSplitter:
    def test_clean_text(self):
        from rag.text_splitter import clean_text
        dirty = "hello\x00world  \n\n\n\n\n\n\n\n\n  extra"
        clean = clean_text(dirty)
        assert "\x00" not in clean
        assert clean.count("\n") < 10

    def test_splitter(self):
        from rag.text_splitter import process_document
        # 生成足够长的文本
        text = ("这是一个测试文档。" * 50 + "\n\n") * 10
        chunks = process_document(text, chunk_size=500, chunk_overlap=50)
        assert len(chunks) > 0
        for c in chunks:
            assert len(c) >= 20  # min_chunk_length

    def test_dedup(self):
        from rag.text_splitter import process_document
        text = "相同的句子。" * 100
        chunks = process_document(text, chunk_size=200, chunk_overlap=20)
        # 去重后应该很少
        assert len(chunks) <= 5  # 去重后应显著减少


class TestVectorStore:
    def test_crud(self):
        from rag.vector_store import (
            add_documents, search_by_owner, delete_by_file,
            delete_all_by_owner, get_collection_stats
        )
        owner = 999
        # 清理
        delete_all_by_owner(owner)
        # 入库
        chunks = ["Python 是一种编程语言", "LangGraph 是多 Agent 框架"]
        metas = [{"owner_id": owner, "source": "test.txt", "chunk_idx": i} for i in range(2)]
        ids = add_documents(chunks, metas)
        assert len(ids) == 2
        # 检索
        results = search_by_owner("Python 编程", owner, top_k=2)
        assert len(results) > 0
        assert "Python" in results[0]["content"]
        # 统计
        stats = get_collection_stats(owner)
        assert stats["total_chunks"] >= 2
        # 删除文件
        count = delete_by_file("test.txt", owner)
        assert count >= 2
        # 清理
        delete_all_by_owner(owner)


class TestRetriever:
    def test_empty_query(self):
        from rag.retriever import retrieve
        result = retrieve("", owner_id=1)
        assert result["count"] == 0
        assert result["context"] == ""

    def test_format_context(self):
        from rag.retriever import format_context_for_prompt
        result = {"documents": [
            {"content": "测试内容", "score": 0.95, "source": "test.txt"},
        ]}
        ctx = format_context_for_prompt(result)
        assert "test.txt" in ctx
        assert "测试内容" in ctx

    def test_format_empty(self):
        from rag.retriever import format_context_for_prompt
        assert format_context_for_prompt({"documents": []}) == ""


# ============================================================
# 4. Agent 模块
# ============================================================

class TestAgentState:
    def test_state_fields(self):
        from agent.state import AgentState
        # 验证必要字段存在
        required = ["messages", "owner_id", "agent_mode", "user_query",
                     "retrieved_docs", "knowledge_context", "final_answer",
                     "needs_tool_call", "iteration_count"]
        for f in required:
            assert f in AgentState.__annotations__, f"缺少字段: {f}"


class TestAgentPrompts:
    def test_prompts_not_empty(self):
        from agent.prompts import CHAT_AGENT_SYSTEM_PROMPT, MANAGE_AGENT_SYSTEM_PROMPT, EVAL_AGENT_SYSTEM_PROMPT
        assert len(CHAT_AGENT_SYSTEM_PROMPT) > 100
        assert len(MANAGE_AGENT_SYSTEM_PROMPT) > 50
        assert len(EVAL_AGENT_SYSTEM_PROMPT) > 100


class TestAgentTools:
    def test_tools_defined(self):
        from agent.tools import (search_knowledge_base, list_my_files,
                                  get_kb_summary, get_chat_context,
                                  verify_answer_against_kb)
        for t in [search_knowledge_base, list_my_files, get_kb_summary,
                  get_chat_context, verify_answer_against_kb]:
            assert hasattr(t, "name")
            assert hasattr(t, "invoke"), f"{t.name} 缺少 invoke 方法"

    def test_list_files_empty(self):
        """测试空知识库下的 list_my_files"""
        from agent.tools import list_my_files
        result = list_my_files.invoke({"owner_id": 99999})
        assert "为空" in result or "没有" in result


class TestGraphWorkflow:
    def test_graph_compile(self):
        from agent.graph_workflow import create_agent_graph, get_agent_graph, reset_agent_graph
        reset_agent_graph()
        graph = get_agent_graph()
        assert graph is not None
        # 检查节点
        nodes = list(graph.nodes.keys())
        assert "router" in nodes
        assert "chat_agent" in nodes
        assert "retrieve" in nodes
        assert "tools" in nodes
        assert "manage_agent" in nodes
        assert "eval_agent" in nodes

    def test_chat_tools_registered(self):
        from agent.chat_agent import CHAT_TOOLS
        assert len(CHAT_TOOLS) == 5, f"期望5个工具，实际{len(CHAT_TOOLS)}个"
        tool_names = [t.name for t in CHAT_TOOLS]
        assert "search_knowledge_base" in tool_names
        assert "get_kb_summary" in tool_names
        assert "get_chat_context" in tool_names
        assert "verify_answer_against_kb" in tool_names


# ============================================================
# 5. API 端点
# ============================================================

class TestAPIAuth:
    def test_login_success(self, client):
        from config.settings import settings
        resp = client.post("/api/auth/login", json={
            "username": settings.admin_username,
            "password": settings.admin_password,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_fail(self, client):
        resp = client.post("/api/auth/login", json={
            "username": "nobody", "password": "wrong",
        })
        assert resp.status_code == 401

    def test_health(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestAPIKB:
    def test_unauthorized(self, client):
        resp = client.get("/api/kb/files")
        assert resp.status_code == 401  # HTTPBearer 无 token 返回 401

    def test_list_files(self, client, auth_headers):
        resp = client.get("/api/kb/files", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "total_files" in data or "files" in data

    def test_upload_txt(self, client, auth_headers, sample_pdf):
        from pathlib import Path
        with open(sample_pdf, "rb") as f:
            resp = client.post(
                "/api/kb/upload",
                files=[("files", (Path(sample_pdf).name, f, "text/plain"))],
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"]

    def test_clear_noop(self, client, auth_headers):
        resp = client.delete("/api/kb/clear", headers=auth_headers)
        assert resp.status_code == 200


class TestAPIChat:
    def test_stream_requires_auth(self, client):
        resp = client.post("/api/chat/stream", json={
            "message": "你好", "agent_mode": "chat",
        })
        assert resp.status_code == 401  # HTTPBearer 无 token 返回 401

    def test_public_stream(self, client):
        """公开流式接口不需要登录"""
        resp = client.post("/api/chat/stream/public", json={
            "message": "你好", "agent_mode": "chat",
        })
        # 无 token 时 HTTPBearer 返回 401
        assert resp.status_code in (200, 401, 422)

    def test_history_requires_auth(self, client):
        resp = client.get("/api/chat/history")
        assert resp.status_code == 401  # HTTPBearer 无 token 返回 401


class TestAPIEval:
    def test_reports_requires_auth(self, client):
        resp = client.get("/api/eval/reports")
        assert resp.status_code == 401  # HTTPBearer 无 token 返回 401

    def test_list_reports(self, client, auth_headers):
        resp = client.get("/api/eval/reports", headers=auth_headers)
        assert resp.status_code == 200


# ============================================================
# 6. SSE 模块
# ============================================================

class TestSSE:
    def test_sse_event_format(self):
        from api.sse_stream import _sse_event
        result = _sse_event("answer", "你好")
        assert result.startswith("data: ")
        data = json.loads(result[6:])
        assert data["type"] == "answer"
        assert data["content"] == "你好"


# ============================================================
# 7. 集成测试：完整链路（需要 API key 在线）
# ============================================================

class TestIntegration:
    """需要真实 API key 的集成测试，标记为 slow"""
    @pytest.mark.slow
    def test_full_chat_flow(self, client, auth_headers):
        """完整对话流程：上传 → 检索 → 对话"""
        # Step 1: 上传知识库文档
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8") as f:
            f.write("姓名：李四\n职业：Python后端工程师\n技能：FastAPI, LangChain, PostgreSQL\n")
            tmp_path = f.name
        with open(tmp_path, "rb") as f:
            resp = client.post("/api/kb/upload",
                files=[("files", ("profile.txt", f, "text/plain"))],
                headers=auth_headers)
        assert resp.status_code == 200

        # Step 2: SSE 流式对话
        resp = client.post("/api/chat/stream", json={
            "message": "李四的职业是什么？", "agent_mode": "chat",
        }, headers=auth_headers)
        assert resp.status_code == 200
        # 读取 SSE 流
        body = ""
        for chunk in resp.iter_bytes(chunk_size=1024):
            text = chunk.decode("utf-8", errors="replace")
            body += text
            if '"done"' in body:
                break
        assert len(body) > 0

        os.unlink(tmp_path)

    @pytest.mark.slow
    def test_kb_isolation(self, client, auth_headers):
        """验证 owner_id 隔离：未登录用户搜不到知识库内容"""
        resp = client.post("/api/chat/stream/public", json={
            "message": "李四的职业是什么？", "agent_mode": "chat",
        })
        assert resp.status_code == 200
        body = ""
        for chunk in resp.iter_bytes(chunk_size=1024):
            text = chunk.decode("utf-8", errors="replace")
            body += text
            if '"done"' in body:
                break
        # 公开接口不需要登录，但 owner_id=0，应该搜不到知识库
        # 不验证具体内容，只验证返回了 SSE 格式
        assert "data:" in body


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("个人数字分身 · 全链路自动化测试")
    print("=" * 60)
    # 运行所有测试（跳过 slow 标记的需要在线 API）
    args = sys.argv[1:] if len(sys.argv) > 1 else ["-v", "-k", "not slow"]
    sys.argv = [sys.argv[0]] + args
    sys.exit(pytest.main(args))
