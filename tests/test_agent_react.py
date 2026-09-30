"""
Agent 层缺陷回归测试（全部离线，不调真实 LLM / 向量库）
=====================================================

参考 ``tests/test_evidence.py`` 的写法：把外部依赖换成本地实现，只做纯逻辑断言。
覆盖本轮修的四个问题：

1. **ReAct 轮次溢出兜底**：永远请求工具的假 LLM 跑到轮次上限时，必须产出
   非空的 ``final_answer``，且它**不是**过渡语（"我先查一下……"）拼接的结果
   —— 否则 ``api/sse_stream.py`` 会把过渡语当答案下发并落库。
2. **工具结果截断**：超长工具返回值塞回 messages 前被截断并带"（内容已截断）"标注。
3. **user_query 只注入一次**：ReAct 循环里追过工具消息后，当前问题不再被重复追加。
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tests.conftest import run_async

# 假 LLM 每轮都会吐出的"过渡语"。它绝不能成为最终答案。
TRANSITIONAL = "我先检索一下相关信息。"


# ============================================================
# 测试替身
# ============================================================


class _AlwaysToolLLM:
    """永远请求工具调用的假 LLM：用来触发 ReAct 轮次溢出。"""

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        return AIMessage(
            content=TRANSITIONAL,
            tool_calls=[
                {
                    "id": "call_x",
                    "name": "search_knowledge_base",
                    "args": {"query": "自我介绍"},
                }
            ],
        )


class _DirectBreaker:
    """直通熔断器：不重试、不熔断，直接 await 目标协程。"""

    async def call(self, func, *args, **kwargs):
        return await func(*args, **kwargs)


class _FakeTool:
    """只提供 name / ainvoke 的假工具，替换 CHAT_TOOLS 用。"""

    def __init__(self, name: str, result):
        self.name = name
        self._result = result

    async def ainvoke(self, args):
        return self._result


def _patch_llm(monkeypatch, llm):
    import agent.chat_agent as ca
    from config.context import get_context

    class _FakeChat:
        def chat_model(self, **_kw):
            return llm

        async def aclose(self):
            return None

    monkeypatch.setattr(get_context(), "chat", _FakeChat())
    monkeypatch.setattr(ca, "llm_circuit_breaker", _DirectBreaker())


def _patch_retrieve(monkeypatch):
    """让 search_knowledge_base 内部的检索返回"无结果"，不走向量库。"""
    import agent.tools as tools_mod

    def fake_retrieve(*args, **kwargs):
        return {
            "documents": [],
            "count": 0,
            "context": "",
            "error": None,
            "degraded": False,
        }

    monkeypatch.setattr(tools_mod, "retrieve", fake_retrieve)


def _fresh_state(user_query: str = "介绍一下你自己") -> dict:
    return {
        "messages": [HumanMessage(content=user_query)],
        "user_query": user_query,
        "owner_id": 1,
        "knowledge_context": "",
        "reasoning_log": [],
        "iteration_count": 0,
        "needs_tool_call": False,
        "final_answer": "",
    }


def _apply(state: dict, update: dict) -> None:
    """模拟 LangGraph 的状态合并（messages / reasoning_log 为追加语义）。"""
    for key, value in update.items():
        if key in ("messages", "reasoning_log"):
            state[key] = list(state.get(key, [])) + list(value)
        else:
            state[key] = value


def _run_react(state: dict, max_steps: int = 30) -> dict:
    """按真实图的流转（chat_agent ⇄ tools）跑完整条 ReAct 循环。"""
    import agent.chat_agent as ca
    from agent.graph_workflow import should_continue_chat

    for _ in range(max_steps):
        _apply(state, run_async(ca.chat_agent_node(state)))
        if should_continue_chat(state) == "end":
            return state
        _apply(state, run_async(ca.tool_executor_node(state)))
    return state


# ============================================================
# 1. ReAct 轮次溢出兜底
# ============================================================


class TestReActOverflow:
    def test_overflow_yields_fallback_not_transitional(self, monkeypatch):
        """跑到轮次上限仍在请求工具时，必须有明确兜底，且不是过渡语。"""
        import agent.chat_agent as ca

        _patch_llm(monkeypatch, _AlwaysToolLLM())
        _patch_retrieve(monkeypatch)

        state = _run_react(_fresh_state())

        final = state.get("final_answer", "")
        assert final.strip(), "溢出了 final_answer 却是空的 —— 上层只能拿过渡语凑答案"
        # 关键：不是过渡语，也不是过渡语的拼接
        assert final != TRANSITIONAL
        assert TRANSITIONAL not in final
        assert final != TRANSITIONAL * ca.MAX_REACT_ITERATIONS
        assert final == ca.REACT_OVERFLOW_ANSWER

        # 溢出被记进 reasoning_log（运维可见）
        assert any("轮次" in line or "兜底" in line for line in state["reasoning_log"])
        # 确实用满了轮次上限
        assert state["iteration_count"] == ca.MAX_REACT_ITERATIONS

    def test_node_at_limit_returns_fallback(self, monkeypatch):
        """直接构造"已达上限且仍要工具"的状态，节点本身要产出兜底。"""
        import agent.chat_agent as ca

        _patch_llm(monkeypatch, _AlwaysToolLLM())
        state = _fresh_state()
        state["iteration_count"] = ca.MAX_REACT_ITERATIONS

        out = run_async(ca.chat_agent_node(state))

        assert out["final_answer"] == ca.REACT_OVERFLOW_ANSWER
        assert out["needs_tool_call"] is False
        assert any("轮次" in line for line in out["reasoning_log"])


class TestShouldContinueChat:
    """路由不再抢先把超限轮次判给 END（那会让 chat_agent 没机会出兜底）。"""

    def test_passes_through_at_limit_so_node_can_fallback(self):
        from agent.graph_workflow import should_continue_chat

        assert should_continue_chat({"needs_tool_call": True, "iteration_count": 5}) == "tools"

    def test_end_when_no_tool_call(self):
        from agent.graph_workflow import should_continue_chat

        assert should_continue_chat({"needs_tool_call": False, "iteration_count": 5}) == "end"

    def test_hard_ceiling_still_ends(self):
        """防御性硬上限：远超上限时强制结束，避免异常状态下的死循环。"""
        from agent.graph_workflow import should_continue_chat

        assert should_continue_chat({"needs_tool_call": True, "iteration_count": 99}) == "end"


# ============================================================
# 2. 工具结果截断
# ============================================================


class TestToolResultTruncation:
    def _ai_with_tool_call(self):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "c1",
                    "name": "search_knowledge_base",
                    "args": {"query": "q"},
                }
            ],
        )

    def test_long_result_truncated_with_mark(self, monkeypatch):
        import agent.chat_agent as ca

        long_text = "字" * (ca.MAX_TOOL_RESULT_CHARS + 999)
        monkeypatch.setattr(ca, "CHAT_TOOLS", [_FakeTool("search_knowledge_base", long_text)])

        out = run_async(
            ca.tool_executor_node({"owner_id": 1, "messages": [self._ai_with_tool_call()]})
        )

        msg = out["messages"][0]
        assert isinstance(msg, ToolMessage)
        assert len(msg.content) < len(long_text), "超长工具结果没有被截断"
        assert msg.content.endswith(ca.TOOL_RESULT_TRUNCATED_MARK)
        assert len(msg.content) == ca.MAX_TOOL_RESULT_CHARS + len(ca.TOOL_RESULT_TRUNCATED_MARK)
        # 截断动作也记进 reasoning_log
        assert any("截断" in line for line in out["reasoning_log"])

    def test_short_result_untouched(self, monkeypatch):
        import agent.chat_agent as ca

        monkeypatch.setattr(ca, "CHAT_TOOLS", [_FakeTool("search_knowledge_base", "短结果")])

        out = run_async(
            ca.tool_executor_node({"owner_id": 1, "messages": [self._ai_with_tool_call()]})
        )

        assert out["messages"][0].content == "短结果"
        assert ca.TOOL_RESULT_TRUNCATED_MARK not in out["messages"][0].content

    def test_boundary_exactly_at_limit_not_truncated(self, monkeypatch):
        import agent.chat_agent as ca

        exact = "字" * ca.MAX_TOOL_RESULT_CHARS
        monkeypatch.setattr(ca, "CHAT_TOOLS", [_FakeTool("search_knowledge_base", exact)])

        out = run_async(
            ca.tool_executor_node({"owner_id": 1, "messages": [self._ai_with_tool_call()]})
        )

        assert out["messages"][0].content == exact
        assert ca.TOOL_RESULT_TRUNCATED_MARK not in out["messages"][0].content


# ============================================================
# 3. user_query 只注入一次
# ============================================================


class TestUserQueryInjectedOnce:
    def test_first_round_injected_once(self):
        import agent.chat_agent as ca

        msgs = run_async(ca.build_chat_messages(_fresh_state("你好")))
        count = sum(1 for m in msgs if isinstance(m, HumanMessage) and m.content == "你好")
        assert count == 1

    def test_not_duplicated_after_tool_rounds(self):
        """循环里追过工具消息后（末条是 ToolMessage），query 仍只出现一次。"""
        import agent.chat_agent as ca

        state = _fresh_state("你好")
        state["messages"] = [
            HumanMessage(content="你好"),
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "search_knowledge_base", "args": {"query": "x"}}],
            ),
            ToolMessage(content="工具结果", tool_call_id="c1"),
        ]

        msgs = run_async(ca.build_chat_messages(state))
        count = sum(1 for m in msgs if isinstance(m, HumanMessage) and m.content == "你好")
        assert count == 1, f"user_query 被重复注入 {count} 次"

    def test_no_duplicate_across_full_loop(self, monkeypatch):
        """整条循环跑完，任何一轮的消息列表里当前问题都只出现一次。"""
        import agent.chat_agent as ca

        _patch_llm(monkeypatch, _AlwaysToolLLM())
        _patch_retrieve(monkeypatch)

        state = _fresh_state("介绍一下你自己")
        seen_counts = []
        from agent.graph_workflow import should_continue_chat

        for _ in range(30):
            msgs = run_async(ca.build_chat_messages(state))
            seen_counts.append(
                sum(
                    1
                    for m in msgs
                    if isinstance(m, HumanMessage) and m.content == state["user_query"]
                )
            )
            _apply(state, run_async(ca.chat_agent_node(state)))
            if should_continue_chat(state) == "end":
                break
            _apply(state, run_async(ca.tool_executor_node(state)))

        assert seen_counts and all(c == 1 for c in seen_counts), seen_counts


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
