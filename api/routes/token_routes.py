"""
Token 使用统计 API 路由
提供按用户、按天聚合的 token 用量和费用统计
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from core.auth import get_current_user
from core.token_tracker import get_usage_stats

router = APIRouter(prefix="/api/token", tags=["Token 统计"])


@router.get("/stats")
async def api_token_stats(
    days: int = Query(default=7, ge=1, le=90, description="统计天数"),
    scope: str = Query(
        default="me",
        pattern="^(me|all)$",
        description="me=只看自己；all=全部用户（仅管理员）",
    ),
    user: dict = Depends(get_current_user),
):
    """
    获取 Token 使用统计：每日分解、模型分解、总费用。

    ``scope=all`` 时统计**全部用户**，并额外返回 ``by_user``（按用户分解）——
    这是管理员视角，普通用户传 all 会 403。

    鉴权放在这一层（数据层只负责"owner_id=None 就查全部"），
    这样"谁能看全部"这条规则只有一处，不会散进查询逻辑里。
    """
    if scope == "all":
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="只有管理员能查看全部用户的用量")
        owner_id = None  # None = 不过滤归属，即全部用户
        message = f"获取 {days} 天内全部用户的 Token 使用统计"
    else:
        owner_id = user["owner_id"]
        message = f"获取 {days} 天内的 Token 使用统计"

    try:
        stats = await get_usage_stats(owner_id=owner_id, days=days)
        return {"success": True, "message": message, "data": stats}
    except Exception as e:
        logger.error(f"[API] Token 统计查询失败: {e}")
        raise HTTPException(status_code=500, detail="查询失败，请稍后重试") from e
