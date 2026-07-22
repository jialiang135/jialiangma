"""
工具注册中心 API
列出所有可用工具，供前端可视化编排面板使用
"""
from fastapi import APIRouter, Depends
from core.tool_registry import tool_registry
from core.auth import get_current_user

router = APIRouter(prefix="/api/tools", tags=["工具"])


@router.get("")
async def list_tools(current_user: dict = Depends(get_current_user)):
    """列出所有注册的工具"""
    return {
        "success": True,
        "tools": tool_registry.list_tools(),
        "categories": tool_registry.list_categories(),
        "total": len(tool_registry),
    }


@router.get("/categories")
async def list_categories(current_user: dict = Depends(get_current_user)):
    """列出所有工具类别"""
    return {
        "success": True,
        "categories": tool_registry.list_categories(),
    }
