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
    user: dict = Depends(get_current_user),
):
    """
    获取 Token 使用统计。
    返回每日分解、模型分解、总费用等信息。
    """
    try:
        stats = await get_usage_stats(owner_id=user["owner_id"], days=days)
        return {
            "success": True,
            "message": f"获取 {days} 天内的 Token 使用统计",
            "data": stats,
        }
    except Exception as e:
        logger.error(f"[API] Token 统计查询失败: {e}")
        raise HTTPException(status_code=500, detail="查询失败，请稍后重试")
