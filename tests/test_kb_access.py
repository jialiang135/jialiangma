"""
知识库访问控制测试
==================

**这些用例是为了防一次真实越权事故复发。**

事故回顾：`agent/chat_agent.py` 把知识库归属**写死成 `owner_id=1`**：

    KB_OWNER_ID = 1                              # 工具里
    retrieve(query=..., owner_id=1)              # 检索里

而 `POST /api/chat/stream/public` 的 docstring 写着"未登录用户无知识库访问权限"、
传下去的 `owner_id=0` —— **这个防护被下层单方面无视了**。
实测：不带任何 token 请求该端点，问"手机号和邮箱是多少"，
直接返回了完整的个人信息（手机号、邮箱、城市、Gitee 主页）。

原来的 `test_all.py::test_kb_isolation` 号称在验证隔离，实际只断言
`"data:" in body` —— **它永远不会因为泄漏而失败**，而且还标了
`@pytest.mark.slow` 默认跳过。**假测试比没有测试更危险**，
它给了"我测过隔离了"的错觉。所以这里做真正可失败的断言。
"""

import asyncio

import pytest

from config.settings import settings
from core.kb_access import can_search_kb, resolve_kb_owner


class TestResolveKbOwner:
    """访问规则的唯一裁决处"""

    def test_anonymous_denied_by_default(self):
        """匿名默认拿不到知识库 —— 这是修复的核心"""
        assert resolve_kb_owner(0) is None
        assert resolve_kb_owner(None) is None
        assert can_search_kb(0) is False

    def test_logged_in_user_shares_admin_kb(self):
        """
        已登录用户共享管理员的知识库 —— 这是**产品本意**：
        面试官注册账号来问分身问题，本来就要能拿到知识库。
        """
        assert resolve_kb_owner(7) == settings.shared_kb_owner_id
        assert resolve_kb_owner(1) == settings.shared_kb_owner_id

    def test_anonymous_allowed_when_explicitly_enabled(self, monkeypatch):
        """显式打开匿名访问时（公开可问的分身场景）才放行"""
        monkeypatch.setattr(settings, "allow_anonymous_kb_access", True)
        assert resolve_kb_owner(0) == settings.shared_kb_owner_id


class TestRetrieveBeforeChat:
    """检索节点必须真的按 owner_id 走，而不是写死"""

    def test_anonymous_skips_retrieval_entirely(self, monkeypatch):
        """
        匿名时**连检索都不该发起** —— 不是"检索了但不给用"，
        而是根本不去碰向量库。这里用哨兵函数证明 retrieve 没被调用。
        """
        import agent.chat_agent as ca

        called = []

        def _sentinel(*args, **kwargs):
            called.append(kwargs)
            return {"documents": [], "count": 0, "context": ""}

        monkeypatch.setattr(ca, "retrieve", _sentinel)

        out = asyncio.run(
            ca.retrieve_before_chat({"owner_id": 0, "user_query": "马佳良的手机号是多少？"})
        )

        assert called == [], "匿名请求竟然发起了知识库检索"
        assert out["knowledge_context"] == ""
        assert out["retrieved_docs"] == []
        assert any("未登录" in line for line in out["reasoning_log"])

    def test_logged_in_retrieval_uses_shared_owner(self, monkeypatch):
        """已登录用户检索时，用的是配置里的共享知识库所有者，不是硬编码 1"""
        import agent.chat_agent as ca

        monkeypatch.setattr(settings, "shared_kb_owner_id", 42)
        seen = {}

        def _capture(*args, **kwargs):
            seen.update(kwargs)
            return {
                "documents": [{"source": "x.md", "score": 0.5, "content": "内容", "chunk_idx": 0}],
                "count": 1,
                "context": "内容",
                "degraded": False,
                "error": None,
            }

        monkeypatch.setattr(ca, "retrieve", _capture)
        out = asyncio.run(ca.retrieve_before_chat({"owner_id": 7, "user_query": "随便问点什么"}))

        assert seen.get("owner_id") == 42, f"检索用的 owner_id 不对: {seen}"
        assert out["retrieved_docs"], "已登录用户应该能拿到检索结果"

    def test_retrieval_error_not_reported_as_empty(self, monkeypatch):
        """
        检索**故障**不能伪装成"知识库里没有"。

        原实现里向量库异常返回空列表，上层于是回答"我的知识库中没有这方面的信息" ——
        用户得到一个自信的错误结论。现在 error 与"确实为空"必须可区分。
        """
        import agent.chat_agent as ca

        monkeypatch.setattr(
            ca,
            "retrieve",
            lambda **kw: {
                "documents": [],
                "count": 0,
                "context": "",
                "error": "Chroma 连接失败",
                "degraded": False,
            },
        )
        out = asyncio.run(ca.retrieve_before_chat({"owner_id": 7, "user_query": "问题"}))

        joined = " ".join(out["reasoning_log"])
        assert "不可用" in joined or "失败" in joined, f"没体现出故障: {joined}"
        assert "未找到相关内容" not in joined, "把检索故障说成了『知识库里没有』"


class TestToolExecutorGuard:
    """工具执行器同样不能把管理员知识库偷偷给匿名用户"""

    def test_anonymous_kb_tool_refused(self):
        """
        `tool_executor_node` 从最后一条消息里取 tool_calls（不是 state 字段），
        所以这里要造一条带 tool_calls 的 AIMessage。
        """
        from langchain_core.messages import AIMessage

        import agent.chat_agent as ca

        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "c1",
                    "name": "search_knowledge_base",
                    "args": {"query": "马佳良的手机号"},
                }
            ],
        )
        out = asyncio.run(ca.tool_executor_node({"owner_id": 0, "messages": [ai_msg]}))

        msgs = out.get("messages") or []
        assert msgs, "应该回一条 ToolMessage 说明被拒绝"
        content = str(getattr(msgs[0], "content", msgs[0]))
        assert "未登录" in content, f"拒绝理由不对: {content}"


class TestKbOwnerIsUnifiedBetweenReadAndWrite:
    """
    知识库的**归属**必须只有一个来源，读和写不能各算各的。

    真实事故：读侧（`/api/kb/files`）用 `settings.shared_kb_owner_id`，
    写侧（上传/删除/清空/重建）用 `user["owner_id"]`。对一个 **admin 角色但
    owner_id ≠ 共享 owner** 的账号（本机实际就有这种账号），两者不是同一个库，
    于是界面显示的是共享库、动的是自己那个空库：

    - 上传 → 文件落进自己的库，聊天检索不到（聊天查共享库）
    - 重建 → 重建空库，日志 "重建结束: 成功 0/0 文件, 0 块"，共享库一点没动
    - 删除 → 拿共享库的文件 ID 比对 owner 不匹配，报"文件不存在"
    - 清空 → 清空自己的库，刷新后文件**还在**（显示的是共享库）

    实测踩到的是"重建 0 个文件"。规则本身在 `core/kb_access.py`，
    这里既验语义、也验**每个端点都真的走了它**（后者才是防回归的关键：
    上传/清空/重建这几条路径很难在单测里真跑，只能盯住调用点）。
    """

    MANAGED = (
        "/api/kb/files",
        "/api/kb/upload",
        "/api/kb/upload-status/{task_id}",
        "/api/kb/files/{file_id}",
        "/api/kb/clear",
        "/api/kb/rebuild",
    )

    def test_helper_targets_shared_owner_not_the_caller(self):
        from api.routes.kb_routes import _kb_owner

        caller_id = 999
        assert caller_id != settings.shared_kb_owner_id, (
            "这个用例的前提是 999 不是共享知识库的 owner，settings 改了就要一起改"
        )
        assert _kb_owner({"owner_id": caller_id, "role": "admin"}) == (
            settings.shared_kb_owner_id
        ), "管理操作必须落到共享知识库，否则会出现「看到的」和「改的」不是同一个库"

    def test_every_kb_endpoint_goes_through_the_authority(self):
        import inspect

        from api.routes import kb_routes

        by_path = {r.path: r for r in kb_routes.router.routes}
        assert set(self.MANAGED) <= set(by_path), (
            f"路由路径变了，用例要同步：缺 {set(self.MANAGED) - set(by_path)}"
        )

        offenders = [
            path
            for path in self.MANAGED
            if "_kb_owner(" not in inspect.getsource(by_path[path].endpoint)
        ]
        assert not offenders, f"这些端点没走 _kb_owner，知识库归属会和读侧不一致: {offenders}"
