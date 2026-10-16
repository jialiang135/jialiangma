"""Behavioral tests for scheduled cleanup, health checks, and database backup."""

import builtins
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

from tests.conftest import run_async


def test_cleanup_old_logs_removes_only_expired_files_and_survives_unlink_errors(
    monkeypatch, tmp_path
):
    import core.scheduler as scheduler

    now = 2_000_000.0
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    old = tmp_path / "old.log"
    locked = tmp_path / "locked.log.1"
    fresh = tmp_path / "fresh.log"
    for path in (old, locked, fresh):
        path.write_text("log")
    os.utime(old, (now - 31 * 86400, now - 31 * 86400))
    os.utime(locked, (now - 31 * 86400, now - 31 * 86400))
    os.utime(fresh, (now, now))

    real_unlink = Path.unlink

    def unlink_with_one_failure(path, *args, **kwargs):
        if path == locked:
            raise OSError("file is in use")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_with_one_failure)
    scheduler.cleanup_old_logs(retention_days=30)

    assert not old.exists()
    assert locked.exists()
    assert fresh.exists()


def test_audit_cleanup_runs_in_main_loop_bridge_and_swallows_bridge_errors(monkeypatch):
    from importlib import import_module

    import core.scheduler as scheduler

    engine_module = import_module("core.db.engine")
    seen = []

    def success(coro):
        seen.append(coro.cr_code.co_name)
        coro.close()
        return 4

    monkeypatch.setattr(engine_module, "run_async_from_thread", success)
    scheduler.cleanup_old_audit_logs(retention_days=12)
    assert seen == ["cleanup_old_audit_logs"]

    def failure(coro):
        coro.close()
        raise RuntimeError("main loop unavailable")

    monkeypatch.setattr(engine_module, "run_async_from_thread", failure)
    scheduler.cleanup_old_audit_logs(retention_days=12)


def test_backup_database_creates_snapshot_and_keeps_only_seven(monkeypatch, tmp_path):
    from importlib import import_module

    import core.scheduler as scheduler
    from core.db.engine import init_database

    engine_module = import_module("core.db.engine")
    run_async(init_database())
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(scheduler, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(scheduler.time, "strftime", lambda _fmt: "20261015_120000")
    for index in range(8):
        (backup_dir / f"personal_agent_2020010{index}_000000.db").write_bytes(b"old")
    monkeypatch.setattr(engine_module, "run_async_from_thread", lambda coro: run_async(coro))

    scheduler.backup_database()

    backups = sorted(backup_dir.glob("personal_agent_*.db"))
    assert len(backups) == 7
    assert (backup_dir / "personal_agent_20261015_120000.db").is_file()

    def fail_backup(coro):
        coro.close()
        raise OSError("disk full")

    monkeypatch.setattr(engine_module, "run_async_from_thread", fail_backup)
    scheduler.backup_database()
    assert len(list(backup_dir.glob("personal_agent_*.db"))) == 7


def test_health_check_reads_and_reports_system_metrics(monkeypatch):
    import psutil

    import core.scheduler as scheduler

    calls = []
    monkeypatch.setattr(psutil, "disk_usage", lambda path: SimpleNamespace(percent=25))
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(percent=40))
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval: calls.append(interval) or 12.5)
    monkeypatch.setattr(psutil, "pids", lambda: [1, 2, 3])

    scheduler.health_check_job()

    assert calls == [1]


def test_scheduler_import_without_optional_dependency_is_supported(monkeypatch):
    import core.scheduler as scheduler

    real_import = builtins.__import__

    def import_without_apscheduler(name, *args, **kwargs):
        if name == "apscheduler.schedulers.background":
            raise ImportError("optional dependency unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_apscheduler)
    importlib.reload(scheduler)
    assert scheduler.APSCHEDULER_AVAILABLE is False

    monkeypatch.undo()
    importlib.reload(scheduler)
    assert scheduler.APSCHEDULER_AVAILABLE is True


def test_scheduler_start_is_idempotent_and_stop_resets_global(monkeypatch):
    import core.scheduler as scheduler

    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "APSCHEDULER_AVAILABLE", False)
    assert scheduler.start_scheduler() is None

    existing = object()
    monkeypatch.setattr(scheduler, "_scheduler", existing)
    monkeypatch.setattr(scheduler, "APSCHEDULER_AVAILABLE", True)
    assert scheduler.start_scheduler() is existing

    created = {}

    class FakeScheduler:
        def __init__(self, *, timezone):
            created["timezone"] = timezone
            created["jobs"] = []
            created["started"] = False
            created["stopped"] = False

        def add_job(self, func, trigger, **kwargs):
            created["jobs"].append((func, trigger, kwargs))

        def start(self):
            created["started"] = True

        def shutdown(self, *, wait):
            created["stopped"] = wait is False

    monkeypatch.setattr(scheduler, "BackgroundScheduler", FakeScheduler)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    started = scheduler.start_scheduler()
    assert started is scheduler._scheduler
    assert created["timezone"] == "Asia/Shanghai"
    assert len(created["jobs"]) == 4
    assert created["started"] is True
    assert scheduler.start_scheduler() is started

    scheduler.stop_scheduler()
    assert created["stopped"] is True
    assert scheduler._scheduler is None
    scheduler.stop_scheduler()
