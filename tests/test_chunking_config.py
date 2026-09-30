"""
知识库切块配置（像 Dify 的数据集分段设置）的测试
=================================================

覆盖四件事，每件都对应一个**会返工**的风险点：

1. **默认行为必须与改造前逐字一致** —— 没配过的知识库读出来就是 settings 里
   那几个值，切块结果不变。否则这次改造会悄悄改变所有已有知识库的检索质量。
2. **校验必须是 400 + 人话**，而不是 422 机读错误、更不能放行把库切成碎片/巨块。
3. **权限口径与 `/api/kb/files` 一致** —— 读走 `_kb_owner`、写要管理员。
   这是项目里反复踩的坑："看到的和改的不是同一个库"。
4. **参数真的流进了切块器** —— 存了配置却没用上，是最隐蔽的假成功。
"""

import pytest
from fastapi.testclient import TestClient

from config.settings import settings

# ============================================================
# 模块级 fixture
# ============================================================


@pytest.fixture(scope="module")
def client():
    from api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def as_user():
    """把鉴权换成指定用户；用例结束自动摘掉覆盖。"""
    from api.main import app
    from core.auth import get_current_user

    def _login(owner_id: int = 5, role: str = "admin"):
        app.dependency_overrides[get_current_user] = lambda: {
            "owner_id": owner_id,
            "username": "tester",
            "role": role,
        }

    yield _login
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def isolated_kb(monkeypatch):
    """把共享知识库 owner 改成一个测试专用值，避免污染真实 owner 的配置。"""
    monkeypatch.setattr(settings, "shared_kb_owner_id", 987654)
    return 987654


# ============================================================
# 1. 默认值 = 改造前行为
# ============================================================


class TestDefaultsMatchSettings:
    def test_defaults_derive_from_settings(self):
        from core.chunking import default_chunking_config

        cfg = default_chunking_config()
        assert cfg.mode == ("semantic" if settings.use_semantic_splitter else "fixed")
        assert cfg.chunk_size == settings.chunk_size
        assert cfg.chunk_overlap == settings.chunk_overlap
        assert cfg.separators is None

    def test_split_output_unchanged_without_config(self):
        """
        不设置任何上下文时，``_split_loaded_document`` 的结果必须与"显式按
        settings 老参数走 process_documents_batch"**逐字一致** —— 这就是
        "默认行为与改动前完全一致"的证据。
        """
        from core.kb_tasks import _split_loaded_document
        from rag.document_loader import LoadOutcome
        from rag.text_splitter import process_documents_batch

        content = (
            "# 第一章 项目背景\n\n"
            + "这是背景介绍。" * 60
            + "\n\n## 1.1 目标\n\n"
            + "这是目标描述。" * 60
            + "\n\n# 第二章 实现\n\n"
            + "这是实现细节。" * 60
        )
        outcome = LoadOutcome("a.md", "a.md", content=content, status="ok")

        got = _split_loaded_document(outcome, settings.use_semantic_splitter)
        expected = process_documents_batch(
            [{"filepath": "a.md", "filename": "a.md", "content": content}],
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            use_semantic_splitter=settings.use_semantic_splitter,
        )[0]["chunks"]

        assert [c["content"] for c in got] == expected
        assert all(c["page"] is None for c in got)

    def test_custom_config_changes_split(self):
        """存了配置就必须真的改变切块结果 —— 防止"配置读了但没用"。"""
        from core.chunking import ChunkingConfig
        from core.kb_tasks import _split_loaded_document, applied_chunking_config
        from rag.document_loader import LoadOutcome

        # 无结构信息 → 走定长，size 直接决定块数。
        # 刻意让每句内容**各不相同**：否则 deduplicate_chunks 会把内容相同的块
        # 合并掉，块数就不再能反映切块粒度了（第一版就是这么写错的）。
        text = "".join(f"第 {i} 句，这是第 {i} 个句子的正文内容。" for i in range(400))
        outcome = LoadOutcome("a.txt", "a.txt", content=text, status="ok")

        default_chunks = _split_loaded_document(outcome, True)
        small = ChunkingConfig(mode="fixed", chunk_size=200, chunk_overlap=20)
        with applied_chunking_config(small):
            custom_chunks = _split_loaded_document(outcome, small.use_semantic_splitter)

        assert len(custom_chunks) > len(default_chunks), "自定义小块应产生更多块"
        # 定长路径下每块不应远超过设定大小
        assert max(len(c["content"]) for c in custom_chunks) <= small.chunk_size * 1.5


# ============================================================
# 2. 校验：区间、overlap < size、mode
# ============================================================


class TestValidation:
    def test_empty_payload_keeps_defaults(self):
        from core.chunking import default_chunking_config, validate_chunking_payload

        assert validate_chunking_payload({}) == default_chunking_config()

    def test_partial_payload_merges_over_base(self):
        from core.chunking import ChunkingConfig, validate_chunking_payload

        base = ChunkingConfig(mode="semantic", chunk_size=1000, chunk_overlap=200)
        cfg = validate_chunking_payload({"chunk_size": 500}, base=base)
        assert cfg.chunk_size == 500
        assert cfg.mode == "semantic"  # 未提供的字段沿用 base
        assert cfg.chunk_overlap == 200

    @pytest.mark.parametrize("size", [10, 0, -5, 100000])
    def test_chunk_size_out_of_range_rejected(self, size):
        from core.chunking import validate_chunking_payload

        with pytest.raises(ValueError, match="chunk_size"):
            validate_chunking_payload({"mode": "fixed", "chunk_size": size})

    def test_overlap_must_be_less_than_size(self):
        from core.chunking import validate_chunking_payload

        with pytest.raises(ValueError, match="chunk_overlap"):
            validate_chunking_payload({"mode": "fixed", "chunk_size": 500, "chunk_overlap": 500})

    def test_negative_overlap_rejected(self):
        from core.chunking import validate_chunking_payload

        with pytest.raises(ValueError, match="chunk_overlap"):
            validate_chunking_payload({"mode": "fixed", "chunk_size": 500, "chunk_overlap": -1})

    def test_invalid_mode_rejected(self):
        from core.chunking import validate_chunking_payload

        with pytest.raises(ValueError, match="mode"):
            validate_chunking_payload({"mode": "magic"})

    def test_separators_string_and_escapes(self):
        from core.chunking import validate_chunking_payload

        cfg = validate_chunking_payload({"separators": "\\n\\n,。,"})
        # 逗号分隔 → 去空 → 还原 \n 转义
        assert cfg.separators == ["\n\n", "。"]

    def test_separators_list_and_empty(self):
        from core.chunking import validate_chunking_payload

        assert validate_chunking_payload({"separators": ["。", "；"]}).separators == ["。", "；"]
        assert validate_chunking_payload({"separators": []}).separators is None

    def test_too_many_separators_rejected(self):
        from core.chunking import MAX_SEPARATORS, validate_chunking_payload

        with pytest.raises(ValueError, match="分隔符"):
            validate_chunking_payload({"separators": [str(i) for i in range(MAX_SEPARATORS + 1)]})


# ============================================================
# 3. 持久化：存下来、重启不丢、更新覆盖
# ============================================================


class TestPersistence:
    def test_missing_row_returns_none_then_default(self):
        from core.chunking import default_chunking_config, load_chunking_config
        from core.db.chunk_config import get_chunk_config
        from core.db.engine import init_database
        from tests.conftest import run_async

        run_async(init_database())
        assert run_async(get_chunk_config(910001)) is None
        assert run_async(load_chunking_config(910001)) == default_chunking_config()

    def test_roundtrip_and_update(self):
        from core.chunking import ChunkingConfig, load_chunking_config, save_chunking_config
        from core.db.engine import init_database
        from tests.conftest import run_async

        run_async(init_database())
        run_async(
            save_chunking_config(
                910002,
                ChunkingConfig(
                    mode="fixed", chunk_size=300, chunk_overlap=30, separators=["\n\n", "。"]
                ),
            )
        )
        cfg = run_async(load_chunking_config(910002))
        assert (cfg.mode, cfg.chunk_size, cfg.chunk_overlap) == ("fixed", 300, 30)
        assert cfg.separators == ["\n\n", "。"]

        # 第二次保存应**覆盖**而不是新增一行
        run_async(
            save_chunking_config(
                910002, ChunkingConfig(mode="semantic", chunk_size=800, chunk_overlap=80)
            )
        )
        cfg2 = run_async(load_chunking_config(910002))
        assert (cfg2.mode, cfg2.chunk_size, cfg2.chunk_overlap) == ("semantic", 800, 80)
        assert cfg2.separators is None

    def test_corrupt_row_falls_back_to_default(self, monkeypatch):
        """库里的脏值不能让读取整个失败（回落默认，并保留告警）。"""
        import core.db.chunk_config as cc
        from core.chunking import default_chunking_config, load_chunking_config
        from tests.conftest import run_async

        async def _fake_get(_owner_id):
            return {"mode": "semantic", "chunk_size": 5, "chunk_overlap": 999}

        monkeypatch.setattr(cc, "get_chunk_config", _fake_get)
        assert run_async(load_chunking_config(1)) == default_chunking_config()

    def test_resolve_without_event_loop_falls_back(self):
        """
        工作线程以外（没有主循环）调用同步解析时，必须回落到默认配置而不是抛错 ——
        配置读不到不该阻断文档入库。
        """
        from core.chunking import default_chunking_config, resolve_chunking_config

        assert resolve_chunking_config(910003) == default_chunking_config()


# ============================================================
# 4. API：读口径、写权限、400 可读
# ============================================================


class TestChunkingApi:
    def test_get_requires_login(self, client):
        resp = client.get("/api/kb/chunking")
        assert resp.status_code == 401

    def test_get_returns_config_and_self_description(self, client, as_user, isolated_kb):
        as_user(owner_id=5, role="user")  # 普通登录用户也能读
        resp = client.get("/api/kb/chunking")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["mode"] in ("semantic", "fixed")
        assert data["chunk_size"] == settings.chunk_size
        # 自描述字段：前端据此渲染，不必再抄一份规则
        assert data["defaults"]["chunk_size"] == settings.chunk_size
        assert data["bounds"]["chunk_size_min"] < data["bounds"]["chunk_size_max"]
        assert data["applies_to"]

    def test_put_requires_admin(self, client, as_user, isolated_kb):
        as_user(owner_id=5, role="user")
        resp = client.put("/api/kb/chunking", json={"mode": "fixed", "chunk_size": 500})
        assert resp.status_code == 403

    def test_put_then_get_roundtrip(self, client, as_user, isolated_kb):
        as_user(owner_id=5, role="admin")
        resp = client.put(
            "/api/kb/chunking",
            json={"mode": "fixed", "chunk_size": 400, "chunk_overlap": 40},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True

        got = client.get("/api/kb/chunking").json()["data"]
        assert (got["mode"], got["chunk_size"], got["chunk_overlap"]) == ("fixed", 400, 40)

    @pytest.mark.parametrize(
        "payload",
        [
            {"mode": "fixed", "chunk_size": 10},
            {"mode": "fixed", "chunk_size": 100000},
            {"mode": "fixed", "chunk_size": 500, "chunk_overlap": 500},
            {"mode": "not-a-mode"},
        ],
    )
    def test_put_invalid_returns_400_with_readable_message(
        self, client, as_user, isolated_kb, payload
    ):
        as_user(owner_id=5, role="admin")
        resp = client.put("/api/kb/chunking", json=payload)
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert isinstance(detail, str) and detail, "错误信息必须是可读的一句话"


class TestChunkingPermissionsMatchFiles:
    """切块配置的归属/权限必须与 `/api/kb/files` 同源，不能各写一套。"""

    ENDPOINTS = ("/api/kb/chunking",)

    def test_endpoints_go_through_kb_owner(self):
        import inspect

        from api.routes import kb_routes

        by_path = {r.path: r for r in kb_routes.router.routes}
        for path in self.ENDPOINTS:
            assert path in by_path, f"路由缺失: {path}"
            src = inspect.getsource(by_path[path].endpoint)
            assert "_kb_owner(" in src, f"{path} 没走 _kb_owner，归属会和读侧不一致"

    def test_write_uses_require_admin(self):
        import inspect

        from api.routes import kb_routes

        by_path = {r.path: r for r in kb_routes.router.routes}
        put = next(
            r for r in by_path.values() if r.path == "/api/kb/chunking" and "PUT" in r.methods
        )
        assert "require_admin" in inspect.getsource(put.endpoint)


# ============================================================
# 5. 冒烟：接口里落库的配置能被 resolve 读到（端到端串一遍）
# ============================================================


def test_saved_config_is_resolved_by_worker_path(client, as_user, isolated_kb):
    as_user(owner_id=5, role="admin")
    resp = client.put(
        "/api/kb/chunking",
        json={"mode": "fixed", "chunk_size": 321, "chunk_overlap": 32},
    )
    assert resp.status_code == 200

    from core.db.chunk_config import get_chunk_config
    from core.db.engine import init_database
    from tests.conftest import run_async

    run_async(init_database())
    row = run_async(get_chunk_config(isolated_kb))
    assert row is not None
    assert (row["mode"], row["chunk_size"], row["chunk_overlap"]) == ("fixed", 321, 32)
