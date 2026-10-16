"""Offline fault injection for streaming, fallback, cancellation, and persistence."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tests.conftest import run_async


class FakeGraph:
    def __init__(self, events=(), stream_error=None, fallback=None, fallback_error=None):
        self.events = events
        self.stream_error = stream_error
        self.fallback = fallback or {}
        self.fallback_error = fallback_error
        self.fallback_states = []

    async def astream_events(self, state, version):
        assert version == "v2"
        for event in self.events:
            yield event
        if self.stream_error:
            raise self.stream_error

    async def ainvoke(self, state):
        self.fallback_states.append(state)
        if self.fallback_error:
            raise self.fallback_error
        return self.fallback


def _fake_persistence(monkeypatch):
    import api.sse_stream as sse

    captured = []

    async def noop():
        return None

    def persist(**kwargs):
        captured.append(kwargs)
        return noop()

    monkeypatch.setattr(sse, "_persist_turn", persist)
    return captured


def _collect(generator):
    async def collect():
        return [json.loads(chunk.removeprefix("data: ")) async for chunk in generator]

    return run_async(collect())


def test_stream_emits_reasoning_tools_usage_and_authoritative_answer(monkeypatch):
    import api.sse_stream as sse

    captured = _fake_persistence(monkeypatch)
    usage_chunk = SimpleNamespace(
        usage_metadata={
            "input_tokens": 3,
            "output_tokens": 4,
            "total_tokens": 7,
            "output_token_details": {"reasoning": 2},
            "input_token_details": {"cache_read": 1},
        }
    )
    events = [
        {"event": "on_custom_event", "name": "ignored"},
        {"event": "on_chat_model_stream", "data": {"chunk": None}},
        {
            "event": "on_chat_model_stream",
            "data": {
                "chunk": SimpleNamespace(
                    content="thinking answer",
                    additional_kwargs={"reasoning_content": "private thought"},
                    content_blocks=[],
                )
            },
        },
        {
            "event": "on_chat_model_stream",
            "data": {"chunk": SimpleNamespace(content="  ", additional_kwargs={}, content_blocks=[])},
        },
        {
            "event": "on_chat_model_stream",
            "data": {"chunk": SimpleNamespace(content=[{"text": "not plain text"}], additional_kwargs={})},
        },
        {"event": "on_chat_model_end", "data": {"output": usage_chunk}},
        {"event": "on_chat_model_end", "data": {"output": SimpleNamespace(usage_metadata=None)}},
        {"event": "on_tool_start", "name": "search", "data": {}},
        {
            "event": "on_tool_end",
            "name": "search",
            "data": {"output": "line one\n" + "x" * 240},
        },
        {"event": "on_chain_end", "name": "empty", "parent_ids": ["root"], "data": {"output": None}},
        {"event": "on_chain_end", "name": "other_node", "parent_ids": ["root"], "data": {"output": {}}},
        {
            "event": "on_chain_end",
            "name": "LangGraph",
            "parent_ids": [],
            "data": {"output": {"reasoning_log": ["duplicated root state"]}},
        },
        {
            "event": "on_chain_end",
            "name": "retrieve",
            "parent_ids": ["root"],
            "data": {
                "output": {
                    "retrieved_docs": [
                        None,
                        {"content": "no source", "score": 0.1},
                        {"content": "evidence", "source": "doc.md", "score": 0.9, "chunk_idx": 0},
                    ],
                    "reasoning_log": ["retrieved", 123],
                }
            },
        },
        {
            "event": "on_chain_end",
            "name": "manage_agent",
            "parent_ids": ["root"],
            "data": {"output": {"final_answer": "authoritative answer", "reasoning_log": ["final step"]}},
        },
    ]
    graph = FakeGraph(events)
    monkeypatch.setattr(sse, "get_agent_graph", lambda: graph)

    events = _collect(
        sse.sse_chat_generator(
            "question", 8, username="member", agent_mode="manage", conversation_id="cid"
        )
    )
    types = [event["type"] for event in events]
    assert types.count("answer") == 1
    assert types.count("reasoning_delta") == 1
    assert types.count("reasoning") == 4  # tool start/end + two valid node log strings
    assert types.count("evidence") == 1
    assert types.count("usage") == 1
    assert types[-1] == "done"
    assert events[0]["content"] == "private thought"
    assert events[1]["content"] == "thinking answer"
    tool_result = next(event["content"] for event in events if event["content"].startswith("📋"))
    assert "line one " in tool_result
    assert len(tool_result.split(": ", 1)[1]) <= 205
    evidence = json.loads(next(event["content"] for event in events if event["type"] == "evidence"))
    assert evidence == [
        {"source": None, "score": 0.1, "content": "no source", "chunk_idx": None},
        {"source": "doc.md", "score": 0.9, "content": "evidence", "chunk_idx": 0},
    ]
    usage = json.loads(next(event["content"] for event in events if event["type"] == "usage"))
    assert usage == {
        "input_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 7,
        "reasoning_tokens": 2,
        "cached_tokens": 1,
        "llm_calls": 1,
    }
    done = json.loads(events[-1]["content"])
    assert done["answer"] == "authoritative answer"
    assert done["conversation_id"] == "cid"
    assert done["usage"] == usage
    assert captured[0]["answer"] == "authoritative answer"
    assert captured[0]["sources"] == ["doc.md"]
    assert captured[0]["thinking"] == "private thought"


def test_empty_stream_falls_back_to_one_shot_answer_and_evidence(monkeypatch):
    import api.sse_stream as sse

    captured = _fake_persistence(monkeypatch)
    graph = FakeGraph(
        stream_error=RuntimeError("stream transport broke"),
        fallback={
            "final_answer": "fallback answer",
            "reasoning_log": ["fallback step", None],
            "retrieved_docs": [{"source": "manual.md", "content": "fact", "score": 0.4}],
        },
    )
    monkeypatch.setattr(sse, "get_agent_graph", lambda: graph)

    events = _collect(sse.sse_chat_generator("question", 2, conversation_id="fallback-id"))
    assert [event["type"] for event in events] == ["answer", "reasoning", "evidence", "reasoning", "done"]
    assert events[0]["content"] == "fallback answer"
    assert events[1]["content"] == "fallback step"
    assert json.loads(events[2]["content"])[0]["source"] == "manual.md"
    assert events[3]["icon"] == "⚠️"
    assert json.loads(events[-1]["content"])["answer"] == "fallback answer"
    assert captured[0]["answer"] == "fallback answer"
    assert graph.fallback_states[0]["user_query"] == "question"


def test_empty_fallback_still_reports_reasoning_and_empty_evidence(monkeypatch):
    import api.sse_stream as sse

    captured = _fake_persistence(monkeypatch)
    graph = FakeGraph(
        stream_error=RuntimeError("stream failed before answer"),
        fallback={"final_answer": "", "reasoning_log": ["retrieval completed"], "retrieved_docs": []},
    )
    monkeypatch.setattr(sse, "get_agent_graph", lambda: graph)

    events = _collect(sse.sse_chat_generator("question", 2))
    assert [event["type"] for event in events] == ["reasoning", "evidence", "reasoning", "done"]
    assert json.loads(events[1]["content"]) == []
    assert json.loads(events[-1]["content"])["answer"] == ""
    assert captured[0]["answer"] == ""


def test_stream_error_after_partial_answer_does_not_retry_whole_agent(monkeypatch):
    import api.sse_stream as sse

    captured = _fake_persistence(monkeypatch)
    chunk = SimpleNamespace(content="partial", additional_kwargs={}, content_blocks=[])
    graph = FakeGraph(
        events=[{"event": "on_chat_model_stream", "data": {"chunk": chunk}}],
        stream_error=RuntimeError("stream ended unexpectedly"),
        fallback={"final_answer": "must not replace partial"},
    )
    monkeypatch.setattr(sse, "get_agent_graph", lambda: graph)

    events = _collect(sse.sse_chat_generator("question", 3))
    assert [event["type"] for event in events] == ["answer", "error", "done"]
    assert events[1]["content"] == "生成过程中断，请重试"
    assert json.loads(events[-1]["content"])["answer"] == "partial"
    assert graph.fallback_states == []
    assert captured[0]["answer"] == "partial"


def test_fallback_failure_returns_error_and_error_done_without_persist(monkeypatch):
    import api.sse_stream as sse

    persist_calls = []
    monkeypatch.setattr(sse, "_persist_turn", lambda **kwargs: persist_calls.append(kwargs))
    graph = FakeGraph(stream_error=RuntimeError("stream failed"), fallback_error=RuntimeError("agent failed"))
    monkeypatch.setattr(sse, "get_agent_graph", lambda: graph)

    events = _collect(sse.sse_chat_generator("question", 4, conversation_id=None))
    assert [event["type"] for event in events] == ["error", "done"]
    assert events[0]["content"] == "服务暂时不可用，请稍后重试"
    assert json.loads(events[1]["content"]) == {
        "status": "error",
        "conversation_id": "",
    }
    assert persist_calls == []


def test_client_cancellation_persists_partial_answer_then_propagates(monkeypatch):
    import api.sse_stream as sse

    captured = _fake_persistence(monkeypatch)
    chunk = SimpleNamespace(content="saved partial", additional_kwargs={}, content_blocks=[])
    graph = FakeGraph(
        events=[{"event": "on_chat_model_stream", "data": {"chunk": chunk}}],
        stream_error=asyncio.CancelledError(),
    )
    monkeypatch.setattr(sse, "get_agent_graph", lambda: graph)

    def run_blocking(coro):
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=2)

    monkeypatch.setattr(sse, "run_async_blocking", run_blocking)

    async def consume():
        try:
            return [item async for item in sse.sse_chat_generator("question", 5)]
        except asyncio.CancelledError:
            return "cancelled"

    assert run_async(consume()) == "cancelled"
    assert captured[0]["answer"] == "saved partial"


def test_cancellation_persistence_failure_does_not_swallow_cancellation(monkeypatch):
    import api.sse_stream as sse

    captured = _fake_persistence(monkeypatch)
    graph = FakeGraph(stream_error=asyncio.CancelledError())
    monkeypatch.setattr(sse, "get_agent_graph", lambda: graph)

    def fail_blocking(coro):
        coro.close()
        raise RuntimeError("persistence worker failed")

    monkeypatch.setattr(sse, "run_async_blocking", fail_blocking)

    async def consume():
        try:
            return [item async for item in sse.sse_chat_generator("question", 5)]
        except asyncio.CancelledError:
            return "cancelled"

    assert run_async(consume()) == "cancelled"
    assert captured[0]["answer"] == ""


def test_normal_persistence_failure_does_not_break_done_event(monkeypatch):
    import api.sse_stream as sse

    async def broken_persistence():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(sse, "_persist_turn", lambda **_kwargs: broken_persistence())
    chunk = SimpleNamespace(content="answer", additional_kwargs={}, content_blocks=[])
    graph = FakeGraph(events=[{"event": "on_chat_model_stream", "data": {"chunk": chunk}}])
    monkeypatch.setattr(sse, "get_agent_graph", lambda: graph)

    events = _collect(sse.sse_chat_generator("question", 6))
    assert events[-1]["type"] == "done"
    assert json.loads(events[-1]["content"])["answer"] == "answer"


def test_persist_turn_skips_blank_answer_but_tracks_spent_tokens(monkeypatch):
    import core.db.chats as chats
    import core.token_tracker as tracker
    from api.sse_stream import _persist_turn

    writes = []
    charges = []

    async def insert(**kwargs):
        writes.append(kwargs)

    async def track(**kwargs):
        charges.append(kwargs)

    monkeypatch.setattr(chats, "insert_chat_log", insert)
    monkeypatch.setattr(tracker, "track_usage", track)
    run_async(
        _persist_turn(
            owner_id=11,
            agent_mode="chat",
            question="q",
            answer="  \n",
            steps=[],
            thinking="",
            sources=[],
            evidence=[],
            conversation_id="cid",
            usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        )
    )
    assert writes == []
    assert charges[0]["owner_id"] == 11
    assert charges[0]["prompt_tokens"] == 2
    assert charges[0]["completion_tokens"] == 3
    assert charges[0]["reasoning_tokens"] == 0


def test_persist_turn_storage_failures_are_isolated_and_thinking_is_bounded(monkeypatch):
    import core.db.chats as chats
    import core.token_tracker as tracker
    from api.sse_stream import MAX_STORED_THINKING, _persist_turn

    charges = []
    stored = []

    async def insert(**kwargs):
        stored.append(kwargs)
        raise RuntimeError("chat database unavailable")

    async def track(**kwargs):
        charges.append(kwargs)
        raise RuntimeError("usage database unavailable")

    monkeypatch.setattr(chats, "insert_chat_log", insert)
    monkeypatch.setattr(tracker, "track_usage", track)
    run_async(
        _persist_turn(
            owner_id=12,
            agent_mode="chat",
            question="q",
            answer="answer",
            steps=["step"],
            thinking="x" * (MAX_STORED_THINKING + 10),
            sources=[],
            evidence=[],
            conversation_id=None,
            usage={"total_tokens": 1},
        )
    )
    assert len(charges) == 1
    assert charges[0]["prompt_tokens"] == 0
    assert charges[0]["cached_tokens"] == 0
    reasoning = json.loads(stored[0]["reasoning"])
    assert len(reasoning["thinking"]) == MAX_STORED_THINKING
