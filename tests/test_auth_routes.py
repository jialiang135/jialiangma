"""Authentication endpoints enforce lockout, registration policy, and user state."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.conftest import run_async


def _sync_to_thread(func, *args, **kwargs):
    async def run():
        return func(*args, **kwargs)

    return run()


def test_auth_requirements_reflect_backend_policy(monkeypatch):
    import api.routes.auth_routes as routes

    monkeypatch.setattr(routes, "settings", SimpleNamespace(allow_registration=True))
    result = run_async(routes.get_auth_requirements())
    assert result.allow_registration is True
    assert result.username_min_length == routes.USERNAME_MIN_LENGTH
    assert result.username_max_length == routes.USERNAME_MAX_LENGTH
    assert result.password_min_length == routes.PASSWORD_MIN_LENGTH
    assert result.password_max_length == routes.PASSWORD_MAX_LENGTH
    assert result.password_require_digit is True
    assert result.password_require_letter is True


def test_login_lockout_stops_before_user_lookup(monkeypatch):
    import api.routes.auth_routes as routes

    async def locked(_username):
        return True

    async def unexpected_lookup(_username):
        raise AssertionError("locked usernames must not be looked up")

    monkeypatch.setattr(routes, "check_login_locked", locked)
    monkeypatch.setattr(routes, "get_user_by_username", unexpected_lookup)
    monkeypatch.setattr(routes, "client_ip", lambda _request: "192.0.2.1")
    monkeypatch.setattr(routes, "settings", SimpleNamespace(login_lockout_minutes=15))

    with pytest.raises(HTTPException) as error:
        run_async(routes.login(SimpleNamespace(username="locked", password="pw"), object()))
    assert error.value.status_code == 429
    assert "15 分钟" in error.value.detail


def test_login_unknown_user_and_bad_password_record_failures(monkeypatch):
    import api.routes.auth_routes as routes

    events = []

    async def unlocked(_username):
        return False

    async def missing(_username):
        return None

    async def wrong_password(_username):
        return {"id": 8, "username": "member", "password_hash": "stored"}

    async def record(username, ip, success):
        events.append((username, ip, success))

    dummy_calls = []

    def dummy_verify():
        dummy_calls.append(True)

    def verify(_password, _stored_hash):
        return False

    monkeypatch.setattr(routes, "check_login_locked", unlocked)
    monkeypatch.setattr(routes, "get_user_by_username", missing)
    monkeypatch.setattr(routes, "record_login_attempt", record)
    monkeypatch.setattr(routes, "dummy_verify", dummy_verify)
    monkeypatch.setattr(routes, "verify_password", verify)
    monkeypatch.setattr(routes, "client_ip", lambda _request: "192.0.2.2")
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=_sync_to_thread))

    with pytest.raises(HTTPException) as unknown:
        run_async(routes.login(SimpleNamespace(username="unknown", password="pw"), object()))
    assert unknown.value.status_code == 401
    assert dummy_calls == [True]
    assert events == [("unknown", "192.0.2.2", False)]

    events.clear()
    monkeypatch.setattr(routes, "get_user_by_username", wrong_password)
    with pytest.raises(HTTPException) as invalid:
        run_async(routes.login(SimpleNamespace(username="member", password="wrong"), object()))
    assert invalid.value.status_code == 401
    assert events == [("member", "192.0.2.2", False)]


def test_login_success_uses_threaded_hash_check_and_defaults_role(monkeypatch):
    import api.routes.auth_routes as routes

    calls = []

    async def unlocked(_username):
        return False

    async def member(_username):
        return {"id": 9, "username": "member", "password_hash": "hash"}

    async def record(username, ip, success):
        calls.append(("attempt", username, ip, success))

    async def audit(action, **kwargs):
        calls.append(("audit", action, kwargs))

    monkeypatch.setattr(routes, "check_login_locked", unlocked)
    monkeypatch.setattr(routes, "get_user_by_username", member)
    monkeypatch.setattr(routes, "record_login_attempt", record)
    monkeypatch.setattr(routes, "verify_password", lambda password, digest: (password, digest) == ("pw", "hash"))
    monkeypatch.setattr(routes, "create_access_token", lambda *args: calls.append(("token", args)) or "jwt")
    monkeypatch.setattr(routes, "log_audit", audit)
    monkeypatch.setattr(routes, "client_ip", lambda _request: "192.0.2.3")
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=_sync_to_thread))

    result = run_async(routes.login(SimpleNamespace(username="member", password="pw"), object()))
    assert result.access_token == "jwt"
    assert (result.owner_id, result.username, result.role) == (9, "member", "user")
    assert ("attempt", "member", "192.0.2.3", True) in calls
    assert ("token", (9, "member", "user")) in calls
    assert any(call[0] == "audit" and call[1] == "login" for call in calls)


def test_current_user_info_returns_not_found_and_role_default(monkeypatch):
    import api.routes.auth_routes as routes

    async def absent(_user_id):
        return None

    monkeypatch.setattr(routes, "get_user_by_id", absent)
    with pytest.raises(HTTPException) as missing:
        run_async(routes.get_current_user_info({"owner_id": 3}))
    assert missing.value.status_code == 404

    async def found(_user_id):
        return {"id": 3, "username": "member", "created_at": "now"}

    monkeypatch.setattr(routes, "get_user_by_id", found)
    assert run_async(routes.get_current_user_info({"owner_id": 3})) == {
        "id": 3,
        "username": "member",
        "role": "user",
        "created_at": "now",
    }


def test_registration_policy_validation_races_and_success(monkeypatch):
    import api.routes.auth_routes as routes

    request = object()
    body = SimpleNamespace(username="member", password="GoodPass123")
    monkeypatch.setattr(routes, "client_ip", lambda _request: "192.0.2.4")
    monkeypatch.setattr(routes, "settings", SimpleNamespace(allow_registration=False))
    with pytest.raises(HTTPException) as disabled:
        run_async(routes.register(body, request))
    assert disabled.value.status_code == 403

    monkeypatch.setattr(routes.settings, "allow_registration", True)
    monkeypatch.setattr(routes, "validate_password_strength", lambda _password: "too weak")
    with pytest.raises(HTTPException) as weak:
        run_async(routes.register(body, request))
    assert weak.value.status_code == 400

    monkeypatch.setattr(routes, "validate_password_strength", lambda _password: None)

    async def existing(_username):
        return {"id": 1}

    monkeypatch.setattr(routes, "get_user_by_username", existing)
    with pytest.raises(HTTPException) as conflict:
        run_async(routes.register(body, request))
    assert conflict.value.status_code == 409

    async def absent(_username):
        return None

    async def duplicate_race(**_kwargs):
        raise ValueError("用户名已被占用")

    monkeypatch.setattr(routes, "get_user_by_username", absent)
    monkeypatch.setattr(routes, "create_user", duplicate_race)
    monkeypatch.setattr(routes, "hash_password", lambda _password: "hash")
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=_sync_to_thread))
    with pytest.raises(HTTPException) as race:
        run_async(routes.register(body, request))
    assert race.value.status_code == 409
    assert race.value.detail == "用户名已被占用"

    events = []

    async def create(**kwargs):
        events.append(("create", kwargs))
        return 12

    async def audit(action, **kwargs):
        events.append(("audit", action, kwargs))

    monkeypatch.setattr(routes, "create_user", create)
    monkeypatch.setattr(routes, "create_access_token", lambda *args: "new-jwt")
    monkeypatch.setattr(routes, "log_audit", audit)
    registered = run_async(routes.register(body, request))
    assert registered.access_token == "new-jwt"
    assert (registered.owner_id, registered.username, registered.role) == (12, "member", "user")
    assert events[0] == (
        "create",
        {"username": "member", "password_hash": "hash", "role": "user"},
    )
    assert events[1][0:2] == ("audit", "register")
