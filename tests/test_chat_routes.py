"""Chat endpoints preserve conversation ownership and stream metadata."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from tests.conftest import run_async


def _body(text="hello", conversation_id=None):
    return SimpleNamespace(message=text, conversation_id=conversation_id, agent_mode="chat")


def _stream(text):
    async def generate(**kwargs):
        yield json.dumps(kwargs, ensure_ascii=False)

    return generate


def _collect_response(response):
    async def collect():
        return [part async for part in response.body_iterator]

    return asyncio.run(collect())


def test_authenticated_stream_generates_or_reuses_conversation_id(monkeypatch):
    import api.routes.chat_routes as routes

    monkeypatch.setattr(routes, "sse_chat_generator", _stream("unused"))
    monkeypatch.setattr(routes.uuid, "uuid4", lambda: UUID("00000000-0000-0000-0000-000000000123"))

    response = run_async(
        routes.chat_stream(_body(), {"owner_id": 7, "username": "member"})
    )
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"
    first = json.loads(_collect_response(response)[0])
    assert first == {
        "user_query": "hello",
        "owner_id": 7,
        "username": "member",
        "agent_mode": "chat",
        "conversation_id": "00000000-0000-0000-0000-000000000123",
    }

    response = run_async(
        routes.chat_stream(_body("continued", "existing-id"), {"owner_id": 7, "username": "member"})
    )
    second = json.loads(_collect_response(response)[0])
    assert second["conversation_id"] == "existing-id"
    assert second["user_query"] == "continued"


def test_public_stream_uses_guest_identity_or_optional_authenticated_user(monkeypatch):
    import api.routes.chat_routes as routes

    monkeypatch.setattr(routes, "sse_chat_generator", _stream("unused"))
    monkeypatch.setattr(routes.uuid, "uuid4", lambda: UUID("00000000-0000-0000-0000-000000000456"))

    guest_response = run_async(routes.chat_stream_public(_body(), None))
    guest = json.loads(_collect_response(guest_response)[0])
    assert (guest["owner_id"], guest["username"]) == (0, "guest")
    assert guest["conversation_id"] == "00000000-0000-0000-0000-000000000456"

    member_response = run_async(
        routes.chat_stream_public(_body("q", "cid"), {"owner_id": 8, "username": "member"})
    )
    member = json.loads(_collect_response(member_response)[0])
    assert (member["owner_id"], member["username"], member["conversation_id"]) == (
        8,
        "member",
        "cid",
    )


def test_history_maps_optional_fields_and_total(monkeypatch):
    import api.routes.chat_routes as routes

    async def history(owner_id, *, limit, offset):
        assert (owner_id, limit, offset) == (9, 2, 3)
        return [
            {
                "id": 1,
                "owner_id": 9,
                "agent_mode": "chat",
                "conversation_id": "cid",
                "question": "q",
                "answer": "a",
                "reasoning": "steps",
                "sources": '["doc"]',
                "created_at": datetime(2025, 1, 2),
            },
            {
                "id": 2,
                "owner_id": 9,
                "agent_mode": "manage",
                "question": "q2",
                "answer": "a2",
                "created_at": datetime(2025, 1, 3),
            },
        ]

    async def count(owner_id):
        assert owner_id == 9
        return 12

    monkeypatch.setattr(routes, "get_chat_history", history)
    monkeypatch.setattr(routes, "get_chat_history_count", count)
    result = run_async(routes.get_history(limit=2, offset=3, user={"owner_id": 9}))
    assert result.total == 12
    assert len(result.conversations) == 2
    assert result.conversations[0].conversation_id == "cid"
    assert result.conversations[0].reasoning == "steps"
    assert result.conversations[0].sources == '["doc"]'
    assert result.conversations[1].conversation_id is None
    assert result.conversations[1].reasoning is None
    assert result.conversations[1].sources is None


def test_legacy_conversation_returns_two_messages_or_empty(monkeypatch):
    import api.routes.chat_routes as routes

    calls = []

    async def lookup(owner_id, log_id):
        calls.append((owner_id, log_id))
        if log_id == 10:
            return {
                "question": "old question",
                "answer": "old answer",
                "reasoning": "thinking",
                "sources": '["source"]',
                "created_at": "2025-01-01T00:00:00",
            }
        return None

    monkeypatch.setattr(routes, "get_chat_log_by_id", lookup)
    found = run_async(routes.get_conversation("__single_10", {"owner_id": 3}))
    assert found["total_turns"] == 1
    assert [message["role"] for message in found["messages"]] == ["user", "assistant"]
    assert found["messages"][1]["reasoning"] == "thinking"
    assert found["messages"][1]["sources"] == '["source"]'

    absent = run_async(routes.get_conversation("__single_11", {"owner_id": 3}))
    assert absent == {"conversation_id": "__single_11", "messages": [], "owner_id": 3}
    assert calls == [(3, 10), (3, 11)]


def test_malformed_legacy_conversation_id_returns_bad_request():
    import api.routes.chat_routes as routes

    with pytest.raises(HTTPException) as exc:
        run_async(routes.get_conversation("__single_not-a-number", {"owner_id": 3}))
    assert exc.value.status_code == 400
    assert exc.value.detail == "对话 ID 无效"


def test_regular_conversation_returns_ordered_messages_and_empty_when_missing(monkeypatch):
    import api.routes.chat_routes as routes

    async def lookup(owner_id, conversation_id):
        assert (owner_id, conversation_id) == (4, "group-id")
        return [
            {
                "question": "q1",
                "answer": "a1",
                "created_at": "t1",
            },
            {
                "question": "q2",
                "answer": "a2",
                "reasoning": "r2",
                "sources": "s2",
                "created_at": "t2",
            },
        ]

    monkeypatch.setattr(routes, "get_chat_by_conversation_id", lookup)
    found = run_async(routes.get_conversation("group-id", {"owner_id": 4}))
    assert found["total_turns"] == 2
    assert [message["role"] for message in found["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert found["messages"][3]["reasoning"] == "r2"
    assert found["messages"][3]["sources"] == "s2"

    async def missing(_owner_id, _conversation_id):
        return []

    monkeypatch.setattr(routes, "get_chat_by_conversation_id", missing)
    assert run_async(routes.get_conversation("absent", {"owner_id": 4})) == {
        "conversation_id": "absent",
        "messages": [],
        "owner_id": 4,
    }


def test_conversation_list_and_delete_success_or_not_found(monkeypatch):
    import api.routes.chat_routes as routes

    async def groups(owner_id, *, limit):
        assert (owner_id, limit) == (5, 7)
        return [
            {
                "group_id": "cid",
                "first_log_id": 3,
                "turn_count": 2,
                "last_at": "now",
                "first_question": "first",
            },
            {"group_id": "legacy", "first_log_id": 4, "turn_count": 1},
        ]

    monkeypatch.setattr(routes, "get_conversations", groups)
    result = run_async(routes.list_conversations(limit=7, user={"owner_id": 5}))
    assert result.total == 2
    assert result.conversations[0].first_question == "first"
    assert result.conversations[1].last_at is None

    async def delete(_owner_id, group_id):
        return 2 if group_id == "cid" else 0

    monkeypatch.setattr(routes, "delete_conversation", delete)
    success = run_async(routes.remove_conversation("cid", {"owner_id": 5, "username": "member"}))
    assert success == {"success": True, "message": "已删除 2 条消息", "deleted": 2}
    absent = run_async(routes.remove_conversation("missing", {"owner_id": 5, "username": "member"}))
    assert absent == {"success": False, "message": "对话不存在或无权删除"}
