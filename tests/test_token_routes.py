"""Token statistics API authorization, filtering, and error behavior."""

import pytest
from fastapi import HTTPException

from tests.conftest import run_async


def test_token_stats_limits_user_scope_and_all_user_scope(monkeypatch):
    import api.routes.token_routes as routes

    calls = []

    async def stats(owner_id, days):
        calls.append((owner_id, days))
        return {"total_tokens": 12}

    monkeypatch.setattr(routes, "get_usage_stats", stats)

    own = run_async(
        routes.api_token_stats(days=14, scope="me", user={"owner_id": 41, "role": "user"})
    )
    assert own == {
        "success": True,
        "message": "获取 14 天内的 Token 使用统计",
        "data": {"total_tokens": 12},
    }

    all_users = run_async(
        routes.api_token_stats(days=30, scope="all", user={"owner_id": 41, "role": "admin"})
    )
    assert all_users == {
        "success": True,
        "message": "获取 30 天内全部用户的 Token 使用统计",
        "data": {"total_tokens": 12},
    }
    assert calls == [(41, 14), (None, 30)]

    with pytest.raises(HTTPException) as denied:
        run_async(routes.api_token_stats(days=7, scope="all", user={"owner_id": 41, "role": "user"}))
    assert denied.value.status_code == 403
    assert calls == [(41, 14), (None, 30)]


def test_token_stats_wraps_repository_errors_as_safe_http_errors(monkeypatch):
    import api.routes.token_routes as routes

    async def fail(**_kwargs):
        raise RuntimeError("database details must not leak")

    monkeypatch.setattr(routes, "get_usage_stats", fail)

    with pytest.raises(HTTPException) as error:
        run_async(routes.api_token_stats(days=7, scope="me", user={"owner_id": 9, "role": "user"}))
    assert error.value.status_code == 500
    assert error.value.detail == "查询失败，请稍后重试"
