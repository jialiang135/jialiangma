"""鉴权必须采用数据库中的当前用户状态，而不是 JWT 签发时的旧角色。"""

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from core.auth import create_access_token, get_current_user


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_current_user_uses_live_role_and_username(monkeypatch):
    """管理员被降权后，旧 JWT 不能继续获得管理员角色。"""
    import core.db.users as user_db

    async def current_user(user_id: int):
        assert user_id == 7
        return {"id": 7, "username": "renamed", "role": "user"}

    monkeypatch.setattr(user_db, "get_user_by_id", current_user)
    stale_admin_token = create_access_token(7, "old-name", "admin")

    user = asyncio.run(get_current_user(_credentials(stale_admin_token)))

    assert user == {"owner_id": 7, "username": "renamed", "role": "user"}


def test_deleted_user_token_is_rejected(monkeypatch):
    import core.db.users as user_db

    async def missing_user(_user_id: int):
        return None

    monkeypatch.setattr(user_db, "get_user_by_id", missing_user)
    token = create_access_token(7, "deleted-user", "admin")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_current_user(_credentials(token)))

    assert exc.value.status_code == 401


def test_malformed_subject_is_rejected_without_database_lookup(monkeypatch):
    import core.db.users as user_db

    async def unexpected_lookup(_user_id: int):
        pytest.fail("malformed JWT subject must be rejected before database lookup")

    monkeypatch.setattr(user_db, "get_user_by_id", unexpected_lookup)
    token = create_access_token("not-an-id", "user", "admin")  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_current_user(_credentials(token)))

    assert exc.value.status_code == 401
