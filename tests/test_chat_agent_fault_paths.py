"""Chat-agent history, fallback, tool authorization, and retrieval outcomes."""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from tests.conftest import run_async


def test_recent_history_uses_owner_caps_turns_and_truncates_answers(monkeypatch):
    import agent.chat_agent as agent
    import core.db.chats as chats

    captured = []

    async def history(owner_id, conversation_id):
        captured.append((owner_id, conversation_id))
        return [
            {"question": f"q{i}", "answer": "a" * (agent.MAX_HISTORY_ANSWER_CHARS + 10)}
            for i in range(agent.MAX_HISTORY_TURNS + 1)
        ]

    monkeypatch.setattr(chats, "get_chat_by_conversation_id", history)
    messages = run_async(
        agent._load_recent_history({"conversation_id": "conversation", "owner_id": 8})
    )
    assert captured == [(8, "conversation")]
    assert len(messages) == agent.MAX_HISTORY_TURNS * 2
    assert messages[0].content == "q1"
    assert len(messages[1].content) == agent.MAX_HISTORY_ANSWER_CHARS

    anonymous = run_async(agent._load_recent_history({"conversation_id": "anonymous"}))
    assert captured[-1] == (0, "anonymous")
    assert len(anonymous) == agent.MAX_HISTORY_TURNS * 2
    assert run_async(agent._load_recent_history({"owner_id": 8})) == []


def test_history_storage_failure_degrades_to_no_history(monkeypatch):
    import agent.chat_agent as agent
    import core.db.chats as chats

    async def fail(*_args):
        raise RuntimeError("history database offline")

    monkeypatch.setattr(chats, "get_chat_by_conversation_id", fail)
    assert run_async(
        agent._load_recent_history({"conversation_id": "conversation", "owner_id": 2})
    ) == []

    async def blank_turn(_owner_id, _conversation_id):
        return [{"question": "  ", "answer": ""}]

    monkeypatch.setattr(chats, "get_chat_by_conversation_id", blank_turn)
    assert run_async(
        agent._load_recent_history({"conversation_id": "conversation", "owner_id": 2})
    ) == []


def test_build_messages_combines_context_history_and_only_supported_message_types(monkeypatch):
    import agent.chat_agent as agent

    assert agent._normalize_refusal_followup("") == ""

    old_question = HumanMessage(content="earlier question")
    old_answer = AIMessage(content="earlier answer")
    monkeypatch.setattr(agent, "_load_recent_history", lambda _state: asyncio.sleep(0, result=[old_question, old_answer]))
    current = HumanMessage(content="follow up")
    tool = ToolMessage(content="tool result", tool_call_id="call-1")
    unsupported = object()

    messages = run_async(
        agent.build_chat_messages(
            {
                "knowledge_context": "retrieved evidence",
                "conversation_id": "cid",
                "user_query": "follow up",
                "messages": [current, tool, unsupported],
            }
        )
    )
    assert isinstance(messages[0], SystemMessage)
    assert "retrieved evidence" in messages[0].content
    assert "此前的对话" in messages[1].content
    assert messages[2:4] == [old_question, old_answer]
    assert messages[4:] == [current, tool]


def test_chat_agent_returns_circuit_and_general_error_fallbacks(monkeypatch):
    import agent.chat_agent as agent
    from config.context import get_context
    from core.circuit_breaker import CircuitOpenError

    class Chat:
        def chat_model(self, **_kwargs):
            class LLM:
                def bind_tools(self, _tools):
                    return self

                async def ainvoke(self, _messages):
                    return AIMessage(content="unexpected")

            return LLM()

    class OpenCircuit:
        async def call(self, *_args, **_kwargs):
            raise CircuitOpenError("llm circuit open")

        def get_fallback_response(self, query):
            return f"fallback:{query}"

    monkeypatch.setattr(get_context(), "chat", Chat())
    monkeypatch.setattr(agent, "llm_circuit_breaker", OpenCircuit())
    state = {"user_query": "question", "owner_id": 1}
    circuit_result = run_async(agent.chat_agent_node(state))
    assert circuit_result["final_answer"] == "fallback:question"
    assert circuit_result["needs_tool_call"] is False
    assert "llm circuit open" in circuit_result["error"]

    class BrokenLLM:
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            raise RuntimeError("provider offline")

    class DirectCircuit:
        async def call(self, func, *args, **kwargs):
            return await func(*args, **kwargs)

    monkeypatch.setattr(get_context(), "chat", SimpleNamespace(chat_model=lambda **_kwargs: BrokenLLM()))
    monkeypatch.setattr(agent, "llm_circuit_breaker", DirectCircuit())
    failed = run_async(agent.chat_agent_node(state))
    assert failed["final_answer"] == "抱歉，AI 服务暂时不可用，请稍后重试。"
    assert failed["error"] == "provider offline"


def test_tool_executor_handles_empty_unknown_failing_and_chat_context_tools(monkeypatch):
    import agent.chat_agent as agent

    empty = run_async(agent.tool_executor_node({"messages": [AIMessage(content="no tools")] }))
    assert empty == {"needs_tool_call": False}

    class FakeTool:
        name = "get_chat_context"

        async def ainvoke(self, args):
            assert args == {"owner_id": 4}
            return {"history": []}

    monkeypatch.setattr(agent, "CHAT_TOOLS", [FakeTool()])
    monkeypatch.setattr(agent, "resolve_kb_owner", lambda _owner: None)
    request = AIMessage(
        content="",
        tool_calls=[{"id": "call-1", "name": "get_chat_context", "args": {}}],
    )
    result = run_async(agent.tool_executor_node({"owner_id": 4, "messages": [request]}))
    assert result["messages"][0].content == "{'history': []}"

    denied_request = AIMessage(
        content="",
        tool_calls=[{"id": "call-2", "name": "search_knowledge_base", "args": {"query": "q"}}],
    )
    denied = run_async(agent.tool_executor_node({"owner_id": 0, "messages": [denied_request]}))
    assert "未登录" in denied["messages"][0].content
    assert "拦截" in denied["reasoning_log"][0]

    class FailingTool:
        name = "known-tool"

        async def ainvoke(self, _args):
            raise RuntimeError("tool unavailable")

    monkeypatch.setattr(agent, "CHAT_TOOLS", [FailingTool()])
    monkeypatch.setattr(agent, "resolve_kb_owner", lambda owner: owner)
    calls = AIMessage(
        content="",
        tool_calls=[
            {"id": "call-3", "name": "unknown-tool", "args": {}},
            {"id": "call-4", "name": "known-tool", "args": {}},
        ],
    )
    result = run_async(agent.tool_executor_node({"owner_id": 9, "messages": [calls]}))
    assert result["messages"][0].content == "未知工具: unknown-tool"
    assert result["messages"][1].content == "工具执行失败: tool unavailable"
    assert len(result["reasoning_log"]) == 2


def test_retrieve_before_chat_distinguishes_blank_anonymous_error_empty_and_success(monkeypatch):
    import agent.chat_agent as agent

    assert run_async(agent.retrieve_before_chat({"user_query": "   ", "owner_id": 2})) == {
        "knowledge_context": "",
        "retrieved_docs": [],
    }

    monkeypatch.setattr(agent, "resolve_kb_owner", lambda _owner: None)
    anonymous = run_async(agent.retrieve_before_chat({"user_query": "question", "owner_id": 0}))
    assert anonymous["retrieved_docs"] == []
    assert "未登录" in anonymous["reasoning_log"][0]

    monkeypatch.setattr(agent, "resolve_kb_owner", lambda owner: owner)
    monkeypatch.setattr(
        agent,
        "retrieve",
        lambda **_kwargs: {"documents": [], "count": 0, "error": "vector store down"},
    )
    failed = run_async(agent.retrieve_before_chat({"user_query": "question", "owner_id": 7}))
    assert "不可用" in failed["reasoning_log"][0]

    monkeypatch.setattr(
        agent,
        "retrieve",
        lambda **_kwargs: {"documents": [], "count": 0, "error": None},
    )
    empty = run_async(agent.retrieve_before_chat({"user_query": "question", "owner_id": 7}))
    assert "未找到" in empty["reasoning_log"][0]

    docs = [{"content": "chunk", "source": "doc.md", "score": "0.9"}]
    monkeypatch.setattr(
        agent,
        "retrieve",
        lambda **_kwargs: {
            "documents": docs,
            "count": 1,
            "context": "formatted context",
            "degraded": True,
        },
    )
    monkeypatch.setattr(agent, "format_context_for_prompt", lambda _result: "formatted context")
    success = run_async(agent.retrieve_before_chat({"user_query": "question", "owner_id": 7}))
    assert success["knowledge_context"] == "formatted context"
    assert success["retrieved_docs"] == docs
    assert "rerank 不可用" in success["reasoning_log"][0]
    assert "未知" in success["reasoning_log"][0]
