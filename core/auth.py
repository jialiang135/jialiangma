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

security_scheme = HTTPBearer(auto_error=False)
optional_security = HTTPBearer(auto_error=False)

# 用于「用户不存在」时消耗与真实 bcrypt 校验相当的时间。
# bcrypt 约 100~300ms，若用户不存在时立刻返回，攻击者能通过响应耗时区分
# "用户名不存在"与"密码错误"，从而枚举出有效用户名。
_DUMMY_HASH = bcrypt.hashpw(b"timing-equalization-dummy", bcrypt.gensalt()).decode()


def dummy_verify() -> None:
    """恒定耗时占位校验，供「用户不存在」分支调用，防止用户名枚举。"""
    bcrypt.checkpw(b"timing-equalization-dummy", _DUMMY_HASH.encode())


def unauthorized(detail: str) -> HTTPException:
    """
    构造 401 未认证响应。

    注意 FastAPI 的 ``HTTPBearer()`` 默认 ``auto_error=True``，缺 Authorization
    头时抛的是 **403 Forbidden**，与 HTTP 语义（应 401，因为根本没提供凭据）
    和新版客户端预期都不符。这里统一改成 401 并带上 ``WWW-Authenticate``。
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


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

def create_access_token(owner_id: int, username: str, role: str = "user") -> str:
    """签发 JWT access token"""
    payload = {
        "sub": str(owner_id),
        "username": username,
        "role": role,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes),
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    return token


def decode_token(token: str) -> dict | None:
    """解码 JWT token，不抛异常"""
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def validate_password_strength(password: str) -> str | None:
    """
    密码强度校验
    返回 None 表示通过，否则返回错误消息
    规则：最小长度 + 至少包含数字和字母
    """
    if len(password) < settings.password_min_length:
        return f"密码至少需要 {settings.password_min_length} 位字符"
    if not any(c.isdigit() for c in password):
        return "密码必须包含至少一个数字"
    if not any(c.isalpha() for c in password):
        return "密码必须包含至少一个字母"
    return None


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
        raise unauthorized("Token 已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise unauthorized("无效的 Token")


# ========================================
# FastAPI 鉴权依赖
# ========================================

async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> dict:
    """
    FastAPI 鉴权依赖注入。
    在所有需要登录的 API 路由中注入此依赖即可。
    返回 {"owner_id": int, "username": str, "role": str}

    缺少凭据时返回 401（而非 FastAPI 默认的 403）。
    """
    if credentials is None:
        raise unauthorized("缺少 Authorization 头")

    payload = verify_token(credentials.credentials)
    owner_id = int(payload.get("sub"))
    username = payload.get("username", "")
    role = payload.get("role", "user")
    logger.debug(f"鉴权通过: owner_id={owner_id}, username={username}, role={role}")
    return {"owner_id": owner_id, "username": username, "role": role}


async def require_admin(
    user: dict = Depends(get_current_user),
) -> dict:
    """
    管理员权限依赖注入。
    在 get_current_user 基础上额外检查 role == 'admin'，
    非管理员返回 403 Forbidden。
    """
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return user


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security),
) -> dict | None:
    """
    可选的鉴权依赖。如果 token 无效不报错，返回 None。
    用于对话页：未登录也可简单对话，但无法访问知识库。
    """
    if credentials is None:
        return None
    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None
