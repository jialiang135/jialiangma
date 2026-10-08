"""
知识库管理节点的测试
======================

这个节点是**确定性**的：不发 LLM、不解析意图，只把知识库现状整理成概况。
``agent_mode="manage"`` 调过来就能用，所以它值得有行为测试 —— 原先前只有一条
"图里有 manage_agent 这个节点"的结构断言，等于没测。

同时钉住一次清理的结论：该节点原带 upload/delete/rebuild/clear 四个分支，
按 ``state["operation"]`` 分发，而 ``operation`` 全项目只被置为 None ——
四个分支**永远走不到**。已删除；下面的用例防止它们被以"补全功能"的名义加回来，
除非同时接上真正的调用点。
"""

import asyncio

from agent.manage_agent import manage_agent_node


def _files(n: int = 3) -> list[dict]:
    return [
        {
            "id": i + 1,
            "filename": f"doc{i}.md",
            "file_size": 1024 * (i + 1),
            "chunk_count": 10 * (i + 1),
            "created_at": "2026-10-08T10:00:00",
        }
        for i in range(n)
    ]


def _patch(monkeypatch, files, chunks=42, unique=3):
    import agent.manage_agent as ma
    import rag.vector_store as vs

    async def _fake_get_files(_owner_id):
        return files

    monkeypatch.setattr(ma, "get_files_by_owner", _fake_get_files)
    monkeypatch.setattr(
        vs, "get_collection_stats", lambda _oid: {"total_chunks": chunks, "unique_files": unique}
    )


class TestManageNodeOverview:
    def test_reports_counts_and_file_details(self, monkeypatch):
        _patch(monkeypatch, _files(3), chunks=60, unique=3)

        out = asyncio.run(manage_agent_node({"owner_id": 1}))

        answer = out["final_answer"]
        assert "知识库概况" in answer
        assert "| 文件数 | 3 |" in answer
        assert "| 向量块总数 | 60 |" in answer
        for i in range(3):
            assert f"doc{i}.md" in answer, "文件明细里必须逐个列出文件名"
        assert out["reasoning_log"], "推理面板要有一句可展示的步骤"
        assert out["messages"], "要给图返回一条 AIMessage，否则对话历史里是空的"

    def test_empty_kb_says_so_and_points_to_the_ui(self, monkeypatch):
        _patch(monkeypatch, [], chunks=0, unique=0)

        answer = asyncio.run(manage_agent_node({"owner_id": 1}))["final_answer"]

        assert "知识库为空" in answer
        assert "上传" in answer, "空的时候要告诉用户去哪儿上传"

    def test_anonymous_is_refused_without_touching_the_kb(self, monkeypatch):
        """
        匿名（缺 owner_id / 为 0）**既不能回落到管理员**，也不该去查库。

        归属统一由 `core.kb_access.resolve_kb_owner` 裁决：匿名默认拿不到知识库。
        这条与聊天检索用的是同一个裁决函数，所以两者不会各说各话。
        """
        seen: list[int] = []

        import agent.manage_agent as ma

        async def _spy(owner_id):
            seen.append(owner_id)
            return []

        monkeypatch.setattr(ma, "get_files_by_owner", _spy)

        out = asyncio.run(manage_agent_node({}))

        assert seen == [], f"匿名不该去查知识库，实际查了 owner={seen}"
        assert "未登录" in out["final_answer"]

    def test_admin_role_with_other_owner_id_sees_the_SHARED_kb(self, monkeypatch):
        """
        **线上实测踩到的那个 bug**：admin 角色但 owner_id ≠ 共享知识库所有者的账号，
        原来看到的是**自己那个空库** —— 于是"知识库页面显示 14 个文件，
        而问它『列出知识库文件』却答『知识库为空』"。

        归属必须走 `resolve_kb_owner`（与 `/api/kb/files`、聊天检索同源），
        而不是 `state["owner_id"]`。
        """
        from config.settings import settings

        seen: list[int] = []

        import agent.manage_agent as ma

        async def _spy(owner_id):
            seen.append(owner_id)
            return _files(2)

        monkeypatch.setattr(ma, "get_files_by_owner", _spy)
        import rag.vector_store as vs

        monkeypatch.setattr(
            vs, "get_collection_stats", lambda _oid: {"total_chunks": 20, "unique_files": 2}
        )

        out = asyncio.run(manage_agent_node({"owner_id": 999}))

        assert seen == [settings.shared_kb_owner_id], (
            f"应当查共享知识库（owner={settings.shared_kb_owner_id}），实际查了 {seen}"
        )
        assert "文件数 | 2" in out["final_answer"]

    def test_does_not_call_the_llm(self, monkeypatch):
        """它是确定性节点。一旦有人往这里塞 LLM 调用，行为就不可复现了。"""
        _patch(monkeypatch, _files(1))

        from config.context import get_context

        class _Boom:
            def chat_model(self, **_kw):
                raise AssertionError("manage 节点不该调用 LLM")

        monkeypatch.setattr(get_context(), "chat", _Boom())

        out = asyncio.run(manage_agent_node({"owner_id": 1}))
        assert out["final_answer"]

    def test_error_is_reported_not_swallowed(self, monkeypatch):
        import agent.manage_agent as ma

        async def _boom(_owner_id):
            raise RuntimeError("数据库挂了")

        monkeypatch.setattr(ma, "get_files_by_owner", _boom)

        out = asyncio.run(manage_agent_node({"owner_id": 1}))

        assert "失败" in out["final_answer"], "出错要说出来，而不是给一份空概况"
        assert any("错误" in s for s in out["reasoning_log"])


class TestUnreachableBranchesAreReallyGone:
    """
    删掉的是"永远走不到"的分支 —— 防止它们被当成"缺失功能"重新加回来。

    要加回来，前提是先有人真正设置 ``state["operation"]``（现在全项目只置 None）。
    这条用例的作用就是逼着后来者面对这个问题。
    """

    def test_no_dead_handlers(self):
        import agent.manage_agent as ma

        for name in ("_handle_upload", "_handle_delete", "_handle_rebuild", "_handle_clear"):
            assert not hasattr(ma, name), (
                f"{name} 又回来了。它依赖 state['operation']，而没有任何地方设置该字段 —— "
                "要么同时接上真实调用点，要么别加。"
            )

    def test_agent_state_has_no_write_only_fields(self):
        """AgentState 里不该留"只写不读"的字段（operation / upload_files / operation_result）。"""
        from agent.state import AgentState

        fields = set(AgentState.__annotations__)
        # 前两个曾因 manage 分支而被写；operation_result 两个 agent 都在写、没人读
        for dead in ("operation", "upload_files", "operation_result"):
            assert dead not in fields, f"{dead} 是只写不读的字段，不该留在状态里"
