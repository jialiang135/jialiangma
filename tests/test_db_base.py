"""UTC-aware database path and datetime conversion boundaries."""

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace


def test_database_path_uses_settings_resolution(monkeypatch, tmp_path):
    import core.db.base as base

    resolved = tmp_path / "configured.sqlite"
    seen = []
    monkeypatch.setattr(
        base,
        "settings",
        SimpleNamespace(db_path="db.sqlite", resolve_path=lambda value: seen.append(value) or resolved),
    )
    assert base.get_db_path() == resolved
    assert seen == ["db.sqlite"]


def test_utc_datetime_bind_and_result_paths():
    import core.db.base as base

    type_ = base.UTCDateTime()
    assert type_.process_bind_param(None, None) is None
    assert type_.process_bind_param("already formatted", None) == "already formatted"
    assert type_.process_bind_param(123, None) == "123"

    aware = datetime(2025, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    assert type_.process_bind_param(aware, None) == "2025-01-01 00:00:00"
    naive = datetime(2025, 1, 1, 0, 0, 0, 999999)
    assert type_.process_bind_param(naive, None) == "2025-01-01 00:00:00"

    existing = datetime(2025, 1, 1)
    assert type_.process_result_value(None, None) is None
    assert type_.process_result_value(existing, None) is existing
    assert type_.process_result_value("2025-01-01 00:00:00.123456", None) == existing
    assert type_.process_result_value("not a date", None) is None


def test_utcnow_is_naive_and_utc():
    import core.db.base as base

    now = base.utcnow()
    assert now.tzinfo is None
    assert abs((datetime.now(UTC).replace(tzinfo=None) - now).total_seconds()) < 2
