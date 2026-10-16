"""Behavioral tests for the database repositories and aggregate views."""

from datetime import timedelta
from uuid import uuid4

import pytest

from tests.conftest import run_async


@pytest.fixture
def initialized_db():
    from core.db.engine import init_database

    run_async(init_database())


def _username() -> str:
    return f"db-coverage-{uuid4().hex}"


def test_chat_history_groups_legacy_rows_and_deletes_by_owner(initialized_db):
    from core.db.chats import (
        delete_conversation,
        get_all_chat_logs,
        get_chat_by_conversation_id,
        get_chat_history,
        get_chat_history_count,
        get_chat_log_by_id,
        get_conversations,
        insert_chat_log,
    )
    from core.db.users import create_user

    username = _username()
    owner_id = run_async(create_user(username, "hash"))
    first = run_async(insert_chat_log(owner_id, "chat", "first", "answer-1", conversation_id="c1"))
    run_async(insert_chat_log(owner_id, "chat", "second", "answer-2", conversation_id="c1"))
    legacy = run_async(insert_chat_log(owner_id, "chat", "legacy", "answer-3"))

    history = run_async(get_chat_history(owner_id))
    assert len(history) == 3
    assert {row["id"] for row in history} == {first, legacy, history[1]["id"]}
    assert run_async(get_chat_history(owner_id, limit=1, offset=1))
    assert run_async(get_chat_history_count(owner_id)) == 3
    assert {row["question"] for row in run_async(get_chat_by_conversation_id(owner_id, "c1"))} == {
        "first",
        "second",
    }
    assert len(run_async(get_all_chat_logs(limit=1))) == 1
    assert run_async(get_chat_log_by_id(owner_id, legacy))["question"] == "legacy"
    assert run_async(get_chat_log_by_id(owner_id + 99999, legacy)) is None

    conversations = run_async(get_conversations(owner_id))
    groups = {row["group_id"]: row for row in conversations}
    assert groups["c1"]["turn_count"] == 2
    assert groups[f"__single_{legacy}"]["turn_count"] == 1
    assert len(run_async(get_all_chat_logs(username=username))) == 3
    assert run_async(get_all_chat_logs(username="missing-user")) == []

    assert run_async(delete_conversation(owner_id + 99999, "c1")) == 0
    assert run_async(delete_conversation(owner_id, f"__single_{legacy}")) == 1
    assert run_async(delete_conversation(owner_id, "c1")) == 2
    assert run_async(get_chat_history_count(owner_id)) == 0


def test_file_and_upload_repositories_enforce_filters_and_whitelists(initialized_db):
    from core.db.files import (
        create_upload_task,
        delete_all_file_records,
        delete_file_record,
        find_file_by_hash_or_name,
        get_all_files,
        get_file_by_id,
        get_files_by_owner,
        get_upload_task,
        insert_file_record,
        update_file_chunk_count,
        update_file_hash,
        update_upload_task,
    )
    from core.db.users import create_user

    username = _username()
    owner_id = run_async(create_user(username, "hash"))
    file_id = run_async(insert_file_record(owner_id, "a.txt", "/tmp/a.txt", 12, 1))
    other_id = run_async(insert_file_record(owner_id, "b.txt", "/tmp/b.txt", 24, 2))
    assert run_async(get_file_by_id(file_id))["filename"] == "a.txt"
    assert run_async(get_file_by_id(-1)) is None
    assert len(run_async(get_files_by_owner(owner_id))) == 2
    assert run_async(find_file_by_hash_or_name(owner_id, "not-found", "a.txt"))["id"] == file_id
    assert run_async(find_file_by_hash_or_name(owner_id, "absent", "absent.txt")) is None

    run_async(update_file_chunk_count(file_id, 7))
    run_async(update_file_hash(file_id, "hash-a"))
    run_async(update_file_hash(other_id, "hash-b"))
    assert run_async(get_file_by_id(file_id))["chunk_count"] == 7
    assert run_async(find_file_by_hash_or_name(owner_id, "hash-a", "renamed.txt"))["id"] == file_id
    assert run_async(find_file_by_hash_or_name(owner_id, "hash-b", "renamed.txt"))["id"] == other_id
    assert run_async(delete_file_record(file_id, owner_id + 99999)) is False
    assert run_async(delete_file_record(file_id, owner_id)) is True
    assert run_async(delete_file_record(file_id, owner_id)) is False
    assert len(run_async(get_all_files(username=username))) == 1
    assert len(run_async(get_all_files())) >= 1
    assert run_async(delete_all_file_records(owner_id)) == 1
    assert run_async(delete_all_file_records(owner_id)) == 0
    assert run_async(get_file_by_id(other_id)) is None

    task_id = f"upload-{uuid4().hex}"
    run_async(create_upload_task(task_id, owner_id, "upload.txt", 4096))
    assert run_async(get_upload_task(task_id, owner_id))["status"] == "pending"
    assert run_async(get_upload_task(task_id, owner_id + 1)) is None
    run_async(
        update_upload_task(
            task_id,
            status="processing",
            progress=60,
            chunk_count=3,
            error="",
            ignored_field="must not persist",
        )
    )
    task = run_async(get_upload_task(task_id, owner_id))
    assert (task["status"], task["progress"], task["chunk_count"]) == ("processing", 60, 3)
    assert "ignored_field" not in task
    run_async(update_upload_task(task_id, ignored_field="no-op"))
    assert run_async(get_upload_task(task_id, owner_id))["progress"] == 60


def test_eval_report_repository_filters_updates_and_omits_large_list_payload(initialized_db):
    from core.db.eval_reports import (
        create_eval_report,
        delete_eval_report,
        get_eval_report,
        list_eval_reports,
        update_eval_report,
    )
    from core.db.users import create_user

    owner_id = run_async(create_user(_username(), "hash"))
    other_owner = run_async(create_user(_username(), "hash"))
    first = run_async(create_eval_report(owner_id, "first", total_questions=2))
    second = run_async(create_eval_report(owner_id, "second", total_questions=3))
    foreign = run_async(create_eval_report(other_owner, "foreign", total_questions=1))

    assert run_async(get_eval_report(first))["status"] == "pending"
    assert run_async(get_eval_report(first, other_owner)) is None
    run_async(
        update_eval_report(
            first,
            status="done",
            progress=100,
            completed=2,
            answered_count=2,
            hallucination_count=0,
            hallucination_rate=0.0,
            honesty_rate=1.0,
            avg_match_score=0.9,
            poor_retrieval_count=0,
            duration_seconds=1.5,
            metrics_json='{"faithfulness": 0.9}',
            results_json='[{"question": "details"}]',
            recommendations_json="[]",
            config_json='{"model": "test"}',
            error="",
            unknown_field="must be ignored",
        )
    )
    updated = run_async(get_eval_report(first, owner_id))
    assert updated["status"] == "done"
    assert updated["hallucination_rate"] == 0.0
    assert updated["honesty_rate"] == 1.0
    assert updated["results_json"] == '[{"question": "details"}]'
    run_async(update_eval_report(first, unknown_field="no-op"))
    assert run_async(get_eval_report(first, owner_id))["progress"] == 100

    run_async(update_eval_report(second, results_json='[{"large": true}]'))
    listed = run_async(list_eval_reports(owner_id=owner_id, limit=1))
    assert len(listed) == 1
    assert listed[0]["id"] == second
    assert "results_json" not in listed[0]
    assert all(row["owner_id"] == owner_id for row in run_async(list_eval_reports(owner_id)))
    assert run_async(list_eval_reports(owner_id=other_owner))[0]["id"] == foreign
    assert run_async(list_eval_reports(owner_id=owner_id, limit=0)) == []

    assert run_async(delete_eval_report(first)) is True
    assert run_async(delete_eval_report(first)) is False
    assert run_async(get_eval_report(first)) is None


def test_user_creation_lookup_lockout_and_aggregates(initialized_db, monkeypatch):
    from config.settings import settings
    from core.db.chats import insert_chat_log
    from core.db.files import insert_file_record
    from core.db.users import (
        check_login_locked,
        create_admin_user,
        create_user,
        get_user_by_id,
        get_user_by_username,
        get_users_with_counts,
        record_login_attempt,
        update_user_role,
    )

    username = _username()
    owner_id = run_async(create_user(username, "hash"))
    assert run_async(get_user_by_username(username))["id"] == owner_id
    assert run_async(get_user_by_id(owner_id))["username"] == username
    assert run_async(get_user_by_username("missing-user")) is None
    assert run_async(get_user_by_id(-1)) is None
    with pytest.raises(ValueError, match="已被占用"):
        run_async(create_user(username, "duplicate"))

    assert run_async(create_admin_user(username, "ignored-hash")) == owner_id
    promoted = run_async(get_user_by_id(owner_id))
    assert promoted["role"] == "admin"
    assert promoted["password_hash"] == "hash"
    assert run_async(update_user_role(owner_id, "user")) is True
    assert run_async(get_user_by_id(owner_id))["role"] == "user"
    assert run_async(update_user_role(owner_id, "admin")) is True
    assert run_async(get_user_by_id(owner_id))["role"] == "admin"
    assert run_async(update_user_role(-1, "user")) is False
    with pytest.raises(ValueError, match="角色只能是 admin 或 user"):
        run_async(update_user_role(owner_id, "root"))

    admin_name = _username()
    admin_id = run_async(create_admin_user(admin_name, "admin-hash"))
    assert run_async(get_user_by_id(admin_id))["role"] == "admin"
    assert run_async(create_admin_user(admin_name, "ignored-hash")) == admin_id
    assert run_async(get_user_by_id(admin_id))["password_hash"] == "admin-hash"

    monkeypatch.setattr(settings, "max_login_attempts", 2)
    monkeypatch.setattr(settings, "login_lockout_minutes", 15)
    run_async(record_login_attempt(username, "127.0.0.1", success=True))
    run_async(record_login_attempt(username, "127.0.0.1", success=False))
    assert run_async(check_login_locked(username)) is False
    run_async(record_login_attempt(username, "127.0.0.1", success=False))
    assert run_async(check_login_locked(username)) is True
    assert run_async(check_login_locked(_username())) is False

    stale_user = _username()

    async def add_stale_attempt():
        from core.db.base import utcnow
        from core.db.engine import session_scope
        from core.db.models import LoginAttempt

        async with session_scope() as session:
            session.add(
                LoginAttempt(
                    username=stale_user,
                    success=0,
                    created_at=utcnow() - timedelta(minutes=20),
                )
            )

    run_async(add_stale_attempt())
    assert run_async(check_login_locked(stale_user)) is False

    run_async(insert_file_record(owner_id, "counts.txt", "/tmp/counts.txt"))
    run_async(insert_chat_log(owner_id, "chat", "question", "answer"))
    counts = {row["id"]: row for row in run_async(get_users_with_counts())}
    assert counts[owner_id]["file_count"] == 1
    assert counts[owner_id]["chat_count"] == 1
    assert counts[admin_id]["file_count"] == 0
    assert counts[admin_id]["chat_count"] == 0


def test_user_cascade_removes_login_attempts_and_related_records(initialized_db):
    from core.db.audit import insert_audit_log
    from core.db.chats import get_chat_history_count
    from core.db.engine import session_scope
    from core.db.models import ChatLog, FileRecord, LoginAttempt, TokenUsage, UploadTask
    from core.db.users import create_user, delete_user_cascade, get_user_by_id

    username = _username()
    owner_id = run_async(create_user(username, "hash"))

    async def add_related_rows():
        async with session_scope() as session:
            session.add_all(
                [
                    ChatLog(owner_id=owner_id, question="q", answer="a"),
                    FileRecord(owner_id=owner_id, filename="f.txt", filepath="/tmp/f.txt"),
                    LoginAttempt(username=username, success=0),
                    TokenUsage(owner_id=owner_id, model="test", prompt_tokens=2),
                    UploadTask(task_id=f"cascade-{uuid4().hex}", owner_id=owner_id, filename="f.txt"),
                ]
            )

    run_async(add_related_rows())
    run_async(insert_audit_log("user.delete", user_id=owner_id, username=username))
    deleted = run_async(delete_user_cascade(owner_id))

    assert deleted == {
        "chat_logs": 1,
        "token_usage": 1,
        "login_attempts": 1,
        "files": 1,
        "upload_tasks": 1,
        "audit_log": 1,
        "users": 1,
    }
    assert run_async(get_user_by_id(owner_id)) is None
    assert run_async(get_chat_history_count(owner_id)) == 0
    empty_delete = run_async(delete_user_cascade(owner_id))
    assert empty_delete["login_attempts"] == 0
    assert empty_delete["users"] == 0


def test_audit_logging_filters_cleanup_and_failure_is_non_fatal(initialized_db, monkeypatch):
    from datetime import UTC, datetime

    import core.db.audit as audit_db
    from core.db.audit import cleanup_old_audit_logs, get_audit_logs, insert_audit_log
    from core.db.engine import session_scope
    from core.db.models import AuditLog

    username = _username()
    run_async(insert_audit_log("file.delete", username=username, resource="file", status="success"))
    run_async(insert_audit_log("chat.create", username=username, resource="chat", status="success"))
    run_async(insert_audit_log("file.delete", username="other", resource="file", status="failed"))

    all_for_user = run_async(get_audit_logs(username=username))
    assert len(all_for_user) == 2
    assert run_async(get_audit_logs())
    assert {row["action"] for row in run_async(get_audit_logs(action="delete", username=username))} == {
        "file.delete"
    }
    assert run_async(get_audit_logs(username="absent-user")) == []

    old_row = AuditLog(
        username=username,
        action="old.action",
        created_at=datetime.now(UTC) - timedelta(days=120),
    )

    async def insert_old_row():
        async with session_scope() as session:
            session.add(old_row)

    run_async(insert_old_row())
    deleted = run_async(cleanup_old_audit_logs(90))
    assert deleted >= 1
    assert not any(row["action"] == "old.action" for row in run_async(get_audit_logs(username=username)))

    class BrokenScope:
        async def __aenter__(self):
            raise RuntimeError("database unavailable")

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(audit_db, "session_scope", BrokenScope)
    run_async(insert_audit_log("best-effort", username=username))


def test_global_stats_aggregate_real_rows_and_skip_unreadable_files(initialized_db, monkeypatch, tmp_path):
    import core.db.stats as stats_db
    from config.settings import settings
    from core.db.chats import insert_chat_log
    from core.db.engine import session_scope
    from core.db.files import insert_file_record
    from core.db.models import TokenUsage
    from core.db.stats import _dir_size_mb, get_global_stats
    from core.db.users import create_user

    uploads = tmp_path / "uploads"
    chroma = tmp_path / "chroma"
    uploads.mkdir()
    chroma.mkdir()
    (uploads / "payload.bin").write_bytes(b"0" * (1024 * 1024))
    (chroma / "index.bin").write_bytes(b"0" * (2 * 1024 * 1024))
    monkeypatch.setattr(settings, "upload_dir", str(uploads))
    monkeypatch.setattr(settings, "chroma_persist_dir", str(chroma))

    before = run_async(get_global_stats())
    owner_id = run_async(create_user(_username(), "hash"))
    run_async(insert_file_record(owner_id, "stats.txt", "/tmp/stats.txt"))
    run_async(insert_chat_log(owner_id, "chat", "stats question", "stats answer"))

    async def add_usage():
        async with session_scope() as session:
            session.add(
                TokenUsage(
                    owner_id=owner_id,
                    model="test",
                    prompt_tokens=11,
                    completion_tokens=13,
                    reasoning_tokens=7,
                    cost_estimate=0.1234567,
                )
            )

    run_async(add_usage())
    after = run_async(get_global_stats())
    assert after["user_count"] == before["user_count"] + 1
    assert after["file_count"] == before["file_count"] + 1
    assert after["total_chats"] == before["total_chats"] + 1
    assert after["today_chats"] >= before["today_chats"] + 1
    assert after["total_tokens"] == before["total_tokens"] + 24
    assert after["total_cost"] == round(before["total_cost"] + 0.1234567, 6)
    assert after["disk_used_mb"] == 1.0
    assert after["chroma_db_mb"] == 2.0
    assert _dir_size_mb(tmp_path / "missing") == 0.0

    monkeypatch.setattr(stats_db.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(stats_db.os, "walk", lambda _path: [("/virtual", [], ["broken.bin"])])

    def raise_oserror(_path):
        raise OSError("unreadable")

    monkeypatch.setattr(stats_db.os.path, "getsize", raise_oserror)
    assert _dir_size_mb(tmp_path / "unreadable") == 0.0
