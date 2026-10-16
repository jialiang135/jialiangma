"""Administrator endpoints must enforce ownership and expose accurate results."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.conftest import run_async

ADMIN = {"owner_id": 1, "username": "admin", "role": "admin"}


def test_list_users_and_dashboard_return_repository_data(monkeypatch):
    import api.routes.admin_routes as routes

    async def users():
        return [{"id": 2, "username": "member", "file_count": 3, "chat_count": 4}]

    async def stats():
        return {"user_count": 2, "file_count": 3}

    monkeypatch.setattr(routes, "get_users_with_counts", users)
    monkeypatch.setattr(routes, "get_global_stats", stats)
    assert run_async(routes.list_users(ADMIN)) == {
        "success": True,
        "total": 1,
        "users": [{"id": 2, "username": "member", "file_count": 3, "chat_count": 4}],
    }
    assert run_async(routes.get_dashboard(ADMIN)) == {"user_count": 2, "file_count": 3}


def test_change_user_role_rejects_self_missing_and_failed_updates(monkeypatch):
    import api.routes.admin_routes as routes

    async def existing(_user_id):
        return {"id": 2, "username": "member", "role": "user"}

    async def missing(_user_id):
        return None

    async def unchanged(_user_id, _role):
        return False

    async def updated(_user_id, _role):
        return True

    body = SimpleNamespace(role="admin")
    with pytest.raises(HTTPException) as self_change:
        run_async(routes.change_user_role(1, body, ADMIN))
    assert self_change.value.status_code == 400

    monkeypatch.setattr(routes, "get_user_by_id", missing)
    with pytest.raises(HTTPException) as not_found:
        run_async(routes.change_user_role(3, body, ADMIN))
    assert not_found.value.status_code == 404

    monkeypatch.setattr(routes, "get_user_by_id", existing)
    monkeypatch.setattr(routes, "update_user_role", unchanged)
    with pytest.raises(HTTPException) as update_failed:
        run_async(routes.change_user_role(2, body, ADMIN))
    assert update_failed.value.status_code == 500

    monkeypatch.setattr(routes, "update_user_role", updated)
    assert run_async(routes.change_user_role(2, body, ADMIN)) == {
        "success": True,
        "message": "用户 member 角色已更新为 admin",
    }


def test_remove_user_protects_admin_and_cleans_vector_files_and_rows(monkeypatch, tmp_path):
    import api.routes.admin_routes as routes
    import rag.vector_store as vector_store

    async def existing(_user_id):
        return {"id": 2, "username": "member", "role": "user"}

    async def files(_user_id):
        return [{"filepath": str(tmp_path / "owned.txt")}, {"filepath": "legacy/path.txt"}]

    async def deleted(_user_id):
        return {"users": 1, "files": 2}

    calls = {"vector": [], "paths": []}

    def delete_vectors(owner_id):
        calls["vector"].append(owner_id)
        return 5

    def remove(root, path):
        calls["paths"].append((root, path))

    async def to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(routes, "get_user_by_id", existing)
    monkeypatch.setattr(routes, "get_files_by_owner", files)
    monkeypatch.setattr(routes, "delete_user_cascade", deleted)
    monkeypatch.setattr(routes, "remove_within", remove)
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(
        routes,
        "settings",
        SimpleNamespace(upload_dir=str(tmp_path), resolve_path=lambda value: Path(value)),
    )
    monkeypatch.setattr(vector_store, "delete_all_by_owner", delete_vectors)

    with pytest.raises(HTTPException) as self_delete:
        run_async(routes.remove_user(1, ADMIN))
    assert self_delete.value.status_code == 400

    result = run_async(routes.remove_user(2, ADMIN))
    assert result == {
        "success": True,
        "message": "用户 member 已删除",
        "cleaned": {"users": 1, "files": 2},
    }
    assert calls["vector"] == [2]
    assert calls["paths"] == [
        (tmp_path, str(tmp_path / "owned.txt")),
        (tmp_path, "legacy/path.txt"),
    ]


def test_remove_user_preserves_records_and_files_when_vector_cleanup_fails(monkeypatch, tmp_path):
    import api.routes.admin_routes as routes
    import rag.vector_store as vector_store

    calls = []

    async def existing(_user_id):
        return {"id": 2, "username": "member", "role": "user"}

    async def files(_user_id):
        return [{"filepath": str(tmp_path / "owned.txt")}]

    async def deleted(_user_id):
        calls.append("database")
        return {"users": 1}

    def fail_vector(_owner_id):
        raise RuntimeError("vector unavailable")

    async def to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(routes, "get_user_by_id", existing)
    monkeypatch.setattr(routes, "get_files_by_owner", files)
    monkeypatch.setattr(routes, "delete_user_cascade", deleted)
    monkeypatch.setattr(routes, "remove_within", lambda *_args: calls.append("disk"))
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(
        routes,
        "settings",
        SimpleNamespace(upload_dir=str(tmp_path), resolve_path=lambda value: Path(value)),
    )
    monkeypatch.setattr(vector_store, "delete_all_by_owner", fail_vector)

    with pytest.raises(HTTPException) as error:
        run_async(routes.remove_user(2, ADMIN))
    assert error.value.status_code == 503
    assert calls == []


def test_remove_user_returns_not_found_without_touching_storage(monkeypatch):
    import api.routes.admin_routes as routes

    async def missing(_user_id):
        return None

    monkeypatch.setattr(routes, "get_user_by_id", missing)
    with pytest.raises(HTTPException) as error:
        run_async(routes.remove_user(22, ADMIN))
    assert error.value.status_code == 404


def test_admin_list_views_shape_optional_fields_and_pass_filters(monkeypatch):
    import api.routes.admin_routes as routes

    async def chats(**kwargs):
        assert kwargs == {"limit": 10, "offset": 3, "username": "member"}
        return [
            {
                "id": 7,
                "owner_id": 2,
                "agent_mode": "chat",
                "question": "q",
                "answer": "a",
                "created_at": "now",
            }
        ]

    async def files(**kwargs):
        assert kwargs == {"limit": 20, "offset": 1, "username": "member"}
        return [
            {"id": 8, "owner_id": 2, "filename": "f.txt", "created_at": "now"}
        ]

    async def audit(**kwargs):
        assert kwargs == {"limit": 5, "action": "delete", "username": "member"}
        return [{"action": "delete", "username": "member"}]

    monkeypatch.setattr(routes, "get_all_chat_logs", chats)
    monkeypatch.setattr(routes, "get_all_files", files)
    monkeypatch.setattr(routes, "db_get_audit_logs", audit)

    chat_result = run_async(routes.list_chat_logs(limit=10, offset=3, username="member", current_user=ADMIN))
    assert chat_result["total"] == 1
    assert chat_result["logs"] == [
        {
            "id": 7,
            "owner_id": 2,
            "username": "",
            "agent_mode": "chat",
            "conversation_id": None,
            "question": "q",
            "answer": "a",
            "reasoning": None,
            "created_at": "now",
        }
    ]

    file_result = run_async(routes.list_all_files(limit=20, offset=1, username="member", current_user=ADMIN))
    assert file_result["files"] == [
        {
            "id": 8,
            "owner_id": 2,
            "username": "",
            "filename": "f.txt",
            "file_size": 0,
            "chunk_count": 0,
            "created_at": "now",
        }
    ]
    assert run_async(routes.get_audit_logs(limit=5, action="delete", username="member", current_user=ADMIN)) == {
        "success": True,
        "total": 1,
        "logs": [{"action": "delete", "username": "member"}],
    }


def test_circuit_queue_and_kb_consistency_admin_views(monkeypatch):
    import api.routes.admin_routes as routes
    import core.kb_tasks as kb_tasks

    class FakeQueue:
        capacity = 12

        def get_queue_size(self):
            return 3

        def get_active_count(self):
            return 2

    monkeypatch.setattr(routes, "async_queue", FakeQueue())
    monkeypatch.setattr(routes.llm_circuit_breaker, "snapshot", lambda: {"name": "llm", "state": "open"})
    monkeypatch.setattr(
        routes.embedding_circuit_breaker,
        "snapshot",
        lambda: {"name": "embedding", "state": "closed"},
    )
    assert run_async(routes.get_queue_status(ADMIN)) == {
        "success": True,
        "queue_size": 3,
        "active": 2,
        "capacity": 12,
    }
    assert run_async(routes.get_circuit_status(ADMIN))["circuits"] == [
        {"name": "llm", "state": "open"},
        {"name": "embedding", "state": "closed"},
    ]

    async def to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(routes.settings, "shared_kb_owner_id", 99)
    consistency_calls = []
    monkeypatch.setattr(
        kb_tasks,
        "check_kb_consistency",
        lambda owner_id: consistency_calls.append(owner_id) or {"consistent": True},
    )
    monkeypatch.setattr(
        kb_tasks,
        "repair_kb_consistency",
        lambda owner_id: {"removed_chunks": 2, "owner_id": owner_id},
    )
    assert run_async(routes.get_kb_consistency(ADMIN)) == {
        "success": True,
        "consistent": True,
    }
    assert consistency_calls == [99]
    assert run_async(routes.repair_kb_consistency_endpoint(ADMIN)) == {
        "success": True,
        "removed_chunks": 2,
        "owner_id": 99,
    }


def test_consistency_endpoints_expose_storage_failures(monkeypatch):
    import api.routes.admin_routes as routes
    import core.kb_tasks as kb_tasks

    async def to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(
        kb_tasks,
        "check_kb_consistency",
        lambda _owner: {"consistent": False, "error": "storage offline"},
    )
    assert run_async(routes.get_kb_consistency(ADMIN)) == {
        "success": False,
        "consistent": False,
        "error": "storage offline",
    }

    def repair_fails(_owner):
        raise RuntimeError("storage offline")

    monkeypatch.setattr(kb_tasks, "repair_kb_consistency", repair_fails)
    with pytest.raises(HTTPException) as error:
        run_async(routes.repair_kb_consistency_endpoint(ADMIN))
    assert error.value.status_code == 503
