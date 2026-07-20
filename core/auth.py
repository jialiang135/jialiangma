"""
JWT 鉴权 + bcrypt 密码哈希工具
"""
import bcrypt
import jwt
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from loguru import logger

from config.settings import settings

security_scheme = HTTPBearer()


# ========================================
# 密码哈希
# ========================================

def hash_password(password: str) -> str:
    """对明文密码进行 bcrypt 哈希"""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """校验明文密码与哈希是否匹配"""
    return bcrypt.checkpw(
        password.encode("utf-8"),
        hashed.encode("utf-8"),
    )


# ========================================
# JWT Token
# ========================================

def create_access_token(owner_id: int, username: str) -> str:
    """签发 JWT access token"""
    payload = {
        "sub": str(owner_id),
        "username": username,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes),
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    return token


def verify_token(token: str) -> dict:
    """校验 JWT token，返回 payload"""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 已过期，请重新登录",
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的 Token",
        )


# ========================================
# FastAPI 鉴权依赖
# ========================================

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
) -> dict:
    """
    FastAPI 鉴权依赖注入。
    在所有需要登录的 API 路由中注入此依赖即可。
    返回 {"owner_id": int, "username": str}
    """
    payload = verify_token(credentials.credentials)
    owner_id = int(payload.get("sub"))
    username = payload.get("username", "")
    logger.debug(f"鉴权通过: owner_id={owner_id}, username={username}")
    return {"owner_id": owner_id, "username": username}


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
) -> dict | None:
    """
    可选的鉴权依赖。如果 token 无效不报错，返回 None。
    用于对话页：未登录也可简单对话，但无法访问知识库。
    """
    try:
        payload = verify_token(credentials.credentials)
        owner_id = int(payload.get("sub"))
        username = payload.get("username", "")
        return {"owner_id": owner_id, "username": username}
    except HTTPException:
        return None
