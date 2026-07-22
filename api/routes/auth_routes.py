"""
登录鉴权路由
"""
from fastapi import APIRouter, Depends, HTTPException, status, Request
from loguru import logger

from core.schemas import UserLogin, UserRegister, TokenResponse, APIResponse
from core.auth import verify_password, create_access_token, hash_password, get_current_user, require_admin, validate_password_strength
from core.database import get_user_by_username, get_user_by_id, create_admin_user, create_user, check_login_locked, record_login_attempt
from core.audit import log_audit
from config.settings import settings

router = APIRouter(prefix="/api/auth", tags=["鉴权"])


@router.post("/login", response_model=TokenResponse)
async def login(body: UserLogin, request: Request):
    """
    管理员登录接口。
    验证用户名密码，返回 JWT Token。
    """
    ip = request.client.host if request.client else "unknown"

    # 检查登录锁定
    if check_login_locked(body.username):
        logger.warning(f"登录锁定: username={body.username}, ip={ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录失败次数过多，请 {settings.login_lockout_minutes} 分钟后重试",
        )

    logger.info(f"登录请求: username={body.username}")

    user = get_user_by_username(body.username)

    if not user:
        # 如果数据库中没有任何用户，且输入匹配配置文件中的管理员凭据，自动创建
        if (body.username == settings.admin_username
                and body.password == settings.admin_password):
            user_id = create_admin_user(
                body.username,
                hash_password(body.password),
            )
            user = get_user_by_username(body.username)
            logger.info(f"首次启动，自动创建管理员账号: {body.username}")
        else:
            record_login_attempt(body.username, ip, success=False)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户名或密码错误",
            )

    # 验证密码
    if not verify_password(body.password, user.get("password_hash", "")):
        record_login_attempt(body.username, ip, success=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    # 登录成功
    record_login_attempt(body.username, ip, success=True)
    user_role = user.get("role", "user")
    token = create_access_token(user["id"], user["username"], user_role)
    log_audit("login", user_id=user["id"], username=user["username"], ip_address=ip, status="success")
    logger.info(f"登录成功: username={body.username}, user_id={user['id']}, role={user_role}")

    return TokenResponse(
        access_token=token,
        owner_id=user["id"],
        username=user["username"],
        role=user_role,
    )


@router.get("/me")
async def get_current_user_info(
    user: dict = Depends(get_current_user),
):
    """获取当前登录用户信息（需要 Bearer Token）"""
    db_user = get_user_by_id(user["owner_id"])
    if not db_user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {
        "id": db_user["id"],
        "username": db_user["username"],
        "role": db_user.get("role", "user"),
        "created_at": db_user["created_at"],
    }


@router.post("/register", response_model=TokenResponse)
async def register(body: UserRegister, request: Request):
    """
    用户注册接口（公开）。
    注册后自动获得普通用户角色，可直接登录使用。
    """
    ip = request.client.host if request.client else "unknown"
    logger.info(f"注册请求: username={body.username}")

    # 密码强度校验
    pwd_error = validate_password_strength(body.password)
    if pwd_error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=pwd_error)

    # 检查用户名是否已存在
    existing = get_user_by_username(body.username)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户名已被占用",
        )

    try:
        user_id = create_user(
            username=body.username,
            password_hash=hash_password(body.password),
            role="user",
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    # 注册成功，直接签发 Token
    token = create_access_token(user_id, body.username, "user")
    log_audit("register", user_id=user_id, username=body.username, ip_address=ip, status="success")
    logger.info(f"注册成功: username={body.username}, user_id={user_id}")

    return TokenResponse(
        access_token=token,
        owner_id=user_id,
        username=body.username,
        role="user",
    )
