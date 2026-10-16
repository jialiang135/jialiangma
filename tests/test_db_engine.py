import asyncio
import sqlite3
import time
from importlib import import_module
from uuid import uuid4

import pytest

from tests.conftest import run_async


def test_run_async_from_thread_closes_coroutine_when_loop_unavailable(monkeypatch):
    from importlib import import_module

    db_engine = import_module("core.db.engine")

    async def operation():
        return None

    coro = operation()
    monkeypatch.setattr(db_engine, "_main_loop", None)

    with pytest.raises(RuntimeError, match="主事件循环未就绪"):
        db_engine.run_async_from_thread(coro)

    assert coro.cr_frame is None


def test_bind_main_loop_records_running_loop(monkeypatch):
    db_engine = import_module("core.db.engine")
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(db_engine, "_main_loop", None)
    try:
        db_engine.bind_main_loop(loop)
        assert db_engine._main_loop is loop
    finally:
        loop.close()


def test_run_async_from_thread_rejects_non_coroutines_and_closed_loops(monkeypatch):
    db_engine = import_module("core.db.engine")
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    monkeypatch.setattr(db_engine, "_main_loop", closed_loop)

    with pytest.raises(RuntimeError, match="主事件循环未就绪"):
        db_engine.run_async_from_thread(object())


def test_cross_thread_bridge_runs_on_main_loop_and_reports_timeout(monkeypatch):
    db_engine = import_module("core.db.engine")

    async def run():
        monkeypatch.setattr(db_engine, "_main_loop", asyncio.get_running_loop())

        async def value():
            return "main-loop-result"

        result = await asyncio.to_thread(db_engine.run_async_from_thread, value(), 1.0)
        assert result == "main-loop-result"

        async def slow():
            await asyncio.sleep(0.05)
            return "late"

        with pytest.raises(TimeoutError):
            await asyncio.to_thread(db_engine.run_async_from_thread, slow(), 0.001)
        await asyncio.sleep(0.06)

    run_async(run())


def test_run_async_blocking_propagates_result_errors_and_timeout():
    db_engine = import_module("core.db.engine")

    async def value():
        return 42

    async def fail():
        raise ValueError("worker failed")

    async def slow():
        await asyncio.sleep(0.03)
        return "late"

    assert db_engine.run_async_blocking(value(), timeout=1) == 42
    with pytest.raises(ValueError, match="worker failed"):
        db_engine.run_async_blocking(fail(), timeout=1)
    with pytest.raises(TimeoutError, match="落库超时"):
        db_engine.run_async_blocking(slow(), timeout=0.001)
    time.sleep(0.04)


def test_session_scope_commits_and_rolls_back():
    db_engine = import_module("core.db.engine")
    from sqlalchemy import select

    from core.db.models import User

    commit_name = f"commit-{uuid4().hex}"
    rollback_name = f"rollback-{uuid4().hex}"

    async def run():
        await db_engine.init_database()
        async with db_engine.session_scope() as session:
            session.add(User(username=commit_name, password_hash="hash"))

        with pytest.raises(RuntimeError, match="roll back"):
            async with db_engine.session_scope() as session:
                session.add(User(username=rollback_name, password_hash="hash"))
                raise RuntimeError("roll back")

        async with db_engine.session_scope() as session:
            committed = await session.execute(select(User).where(User.username == commit_name))
            rolled_back = await session.execute(select(User).where(User.username == rollback_name))
            assert committed.scalar_one_or_none() is not None
            assert rolled_back.scalar_one_or_none() is None

    run_async(run())


def test_sqlite_connections_apply_wal_busy_timeout_and_normal_sync():
    db_engine = import_module("core.db.engine")

    async def run():
        async with db_engine.engine.connect() as connection:
            journal = await connection.exec_driver_sql("PRAGMA journal_mode")
            busy = await connection.exec_driver_sql("PRAGMA busy_timeout")
            synchronous = await connection.exec_driver_sql("PRAGMA synchronous")
            assert journal.scalar_one().lower() == "wal"
            assert busy.scalar_one() == 5000
            assert synchronous.scalar_one() == 1

    run_async(run())


def test_dispose_engine_releases_connections():
    db_engine = import_module("core.db.engine")
    run_async(db_engine.dispose_engine())
    run_async(db_engine.init_database())


def test_get_session_dependency_commits_after_generator_resumes():
    db_engine = import_module("core.db.engine")
    from sqlalchemy import select

    from core.db.models import User

    username = f"dependency-{uuid4().hex}"

    async def run():
        await db_engine.init_database()
        dependency = db_engine.get_session()
        session = await anext(dependency)
        session.add(User(username=username, password_hash="hash"))
        with pytest.raises(StopAsyncIteration):
            await anext(dependency)

        async with db_engine.session_scope() as check:
            result = await check.execute(select(User).where(User.username == username))
            assert result.scalar_one_or_none() is not None

    run_async(run())


def test_init_database_migrates_legacy_columns(tmp_path, monkeypatch):
    db_engine = import_module("core.db.engine")
    from config.settings import settings

    legacy_path = tmp_path / "legacy.db"
    with sqlite3.connect(legacy_path) as connection:
        connection.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT)"
        )
        connection.execute(
            "CREATE TABLE files (id INTEGER PRIMARY KEY, owner_id INTEGER, filename TEXT, filepath TEXT)"
        )
        connection.execute(
            "CREATE TABLE token_usage (id INTEGER PRIMARY KEY, owner_id INTEGER, model TEXT, "
            "prompt_tokens INTEGER, completion_tokens INTEGER, cost_estimate REAL, created_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE eval_reports (id INTEGER PRIMARY KEY, owner_id INTEGER, testset_name TEXT)"
        )

    original_engine = db_engine.engine
    monkeypatch.setattr(settings, "db_path", str(legacy_path))
    legacy_engine = db_engine._build_engine()
    monkeypatch.setattr(db_engine, "engine", legacy_engine)
    try:
        run_async(db_engine.init_database())
    finally:
        run_async(legacy_engine.dispose())

    with sqlite3.connect(legacy_path) as connection:
        columns = {
            table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for table in ("users", "files", "token_usage", "eval_reports")
        }
    monkeypatch.setattr(db_engine, "engine", original_engine)
    assert "role" in columns["users"]
    assert "file_hash" in columns["files"]
    assert {"reasoning_tokens", "cached_tokens"} <= columns["token_usage"]
    assert {
        "status",
        "progress",
        "answered_count",
        "honesty_rate",
        "duration_seconds",
        "metrics_json",
        "config_json",
        "error",
    } <= columns["eval_reports"]


def test_online_backup_creates_and_replaces_quoted_path(tmp_path):
    db_engine = import_module("core.db.engine")
    destination = tmp_path / "backup's copy.db"
    run_async(db_engine.init_database())

    async def source_user_count():
        async with db_engine.engine.connect() as connection:
            result = await connection.exec_driver_sql("SELECT COUNT(*) FROM users")
            return result.scalar_one()

    expected_users = run_async(source_user_count())
    run_async(db_engine.backup_database(destination))
    assert destination.is_file()
    with sqlite3.connect(destination) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "users" in tables
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == expected_users

    destination.write_bytes(b"stale backup")
    run_async(db_engine.backup_database(destination))
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == expected_users


def test_startup_marks_interrupted_tasks_failed():
    db_engine = import_module("core.db.engine")
    from sqlalchemy import select

    from core.db.models import EvalReport, UploadTask

    first_owner = 700_000_000 + uuid4().int % 100_000_000
    owners = (first_owner, first_owner + 1, first_owner + 2)
    task_ids = (f"stale-pending-{uuid4().hex}", f"stale-processing-{uuid4().hex}", f"done-{uuid4().hex}")

    async def insert_rows():
        await db_engine.init_database()
        async with db_engine.session_scope() as session:
            session.add_all(
                [
                    EvalReport(owner_id=owners[0], testset_name="pending", status="pending"),
                    EvalReport(owner_id=owners[1], testset_name="running", status="running"),
                    EvalReport(owner_id=owners[2], testset_name="done", status="completed"),
                    UploadTask(task_id=task_ids[0], owner_id=owners[0], filename="a", status="pending"),
                    UploadTask(task_id=task_ids[1], owner_id=owners[1], filename="b", status="processing"),
                    UploadTask(task_id=task_ids[2], owner_id=owners[2], filename="c", status="completed"),
                ]
            )

    run_async(insert_rows())
    fixed = run_async(db_engine.fail_stale_tasks())
    assert fixed["eval_reports"] >= 2
    assert fixed["upload_tasks"] >= 2

    async def fetch():
        async with db_engine.session_scope() as session:
            reports = (
                await session.execute(select(EvalReport).where(EvalReport.owner_id.in_(owners)))
            ).scalars().all()
            tasks = (
                await session.execute(select(UploadTask).where(UploadTask.task_id.in_(task_ids)))
            ).scalars().all()
            return reports, tasks

    reports, tasks = run_async(fetch())
    interrupted = [row for row in reports if row.status == "failed"]
    assert len(interrupted) == 2
    assert all(row.progress == 100 and "中断" in row.error for row in interrupted)
    interrupted_uploads = [row for row in tasks if row.status == "failed"]
    assert len(interrupted_uploads) == 2
    assert all(row.progress == 100 and "中断" in row.error for row in interrupted_uploads)
    assert run_async(db_engine.fail_stale_tasks()) == {"eval_reports": 0, "upload_tasks": 0}
