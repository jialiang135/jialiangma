"""
登录鉴权路由
"""
from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger

from core.schemas import UserLogin, TokenResponse, APIResponse
from core.auth import verify_password, create_access_token, hash_password, get_current_user
from core.database import get_user_by_username, get_user_by_id, create_admin_user
from config.settings import settings

router = APIRouter(prefix="/api/auth", tags=["鉴权"])


@router.post("/login", response_model=TokenResponse)
async def login(body: UserLogin):
    """
    管理员登录接口。
    验证用户名密码，返回 JWT Token。
    """
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
            user = {"id": user_id, "username": body.username}
            logger.info(f"首次启动，自动创建管理员账号: {body.username}")
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户名或密码错误",
            )

    # 验证密码
    if not verify_password(body.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    # 签发 Token
    token = create_access_token(user["id"], user["username"])
    logger.info(f"登录成功: username={body.username}, user_id={user['id']}")

    return TokenResponse(
        access_token=token,
        owner_id=user["id"],
        username=user["username"],
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
        "created_at": db_user["created_at"],
    }
