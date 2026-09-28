"""
管理员运维接口 — 用户管理 / 仪表盘 / 审计日志 / 熔断状态 / 队列状态
"""
import asyncio
import os
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from core.auth import require_admin
from core.circuit_breaker import llm_circuit_breaker, embedding_circuit_breaker
from core.async_queue import async_queue
from core.schemas import (
    DashboardStats, UserAdminOut, UserRoleUpdate,
    ChatLogAdminOut, FileAdminOut, APIResponse,
)
from core.database import (
    get_users_with_counts, update_user_role, delete_user_cascade,
    get_all_chat_logs, get_all_files, get_global_stats,
    get_user_by_id, get_files_by_owner,
    # 别名：本模块的路由函数也叫 get_audit_logs，直接同名导入会被它覆盖
    get_audit_logs as db_get_audit_logs,
)
from core.paths import remove_within

router = APIRouter(prefix="/api/admin", tags=["运维"])


# ═══════════════════════════════════════════
# 用户管理
# ═══════════════════════════════════════════

@router.get("/users")
async def list_users(current_user: dict = Depends(require_admin)):
    """
    列出所有用户（含文件数、对话数统计）。
    仅管理员可调用。
    """
    # 单次聚合查询（原实现是每用户各开两次连接统计的 N+1）
    users = await get_users_with_counts()
    return {
        "success": True,
        "total": len(users),
        "users": users,
    }


@router.put("/users/{user_id}/role")
async def change_user_role(
    user_id: int,
    body: UserRoleUpdate,
    current_user: dict = Depends(require_admin),
):
    """修改用户角色（admin/user）。不能修改自己。"""
    if user_id == current_user["owner_id"]:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")

    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    updated = await update_user_role(user_id, body.role)
    if not updated:
        raise HTTPException(status_code=500, detail="更新失败")

    logger.info(
        f"[Admin] 用户角色变更: {current_user['username']} 将 {user['username']} "
        f"({user_id}) 从 {user['role']} 改为 {body.role}"
    )
    return {
        "success": True,
        "message": f"用户 {user['username']} 角色已更新为 {body.role}",
    }


@router.delete("/users/{user_id}")
async def remove_user(
    user_id: int,
    current_user: dict = Depends(require_admin),
):
    """删除用户及其所有关联数据（知识库、对话记录、Token 统计等）。不能删除自己。"""
    if user_id == current_user["owner_id"]:
        raise HTTPException(status_code=400, detail="不能删除自己")

    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 清理向量库数据（Chroma 是同步的，丢线程池）
    try:
        from rag.vector_store import delete_all_by_owner
        await asyncio.to_thread(delete_all_by_owner, user_id)
    except Exception as e:
        logger.warning(f"[Admin] 清理用户 {user_id} 向量库失败: {e}")

    # 清理上传文件
    from config.settings import settings
    upload_dir = settings.resolve_path(settings.upload_dir)
    for record in await get_files_by_owner(user_id):
        # remove_within 保证只删 upload_dir 内的文件：
        # files.filepath 可能存着早期版本未净化文件名时写入的越界路径
        remove_within(upload_dir, record["filepath"])

    counts = await delete_user_cascade(user_id)

    logger.info(
        f"[Admin] 用户删除: {current_user['username']} 删除了 {user['username']} "
        f"({user_id}), 清理数据: {counts}"
    )
    return {
        "success": True,
        "message": f"用户 {user['username']} 已删除",
        "cleaned": counts,
    }


# ═══════════════════════════════════════════
# 仪表盘
# ═══════════════════════════════════════════

@router.get("/dashboard", response_model=DashboardStats)
async def get_dashboard(current_user: dict = Depends(require_admin)):
    """
    全局数据总览仪表盘。
    返回用户数、文件数、今日对话、Token 消耗、磁盘用量等关键指标。
    """
    return await get_global_stats()


# ═══════════════════════════════════════════
# 全局对话查询
# ═══════════════════════════════════════════

@router.get("/chat-logs")
async def list_chat_logs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    username: str = None,
    current_user: dict = Depends(require_admin),
):
    """跨用户查询对话记录（可按用户名筛选）"""
    logs = await get_all_chat_logs(limit=limit, offset=offset, username=username)
    return {
        "success": True,
        "total": len(logs),
        "logs": [
            {
                "id": log["id"],
                "owner_id": log["owner_id"],
                "username": log.get("username", ""),
                "agent_mode": log["agent_mode"],
                "conversation_id": log.get("conversation_id"),
                "question": log["question"],
                "answer": log["answer"],
                "reasoning": log.get("reasoning"),
                "created_at": log["created_at"],
            }
            for log in logs
        ],
    }


# ═══════════════════════════════════════════
# 全局文件查询
# ═══════════════════════════════════════════

@router.get("/files")
async def list_all_files(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    username: str = None,
    current_user: dict = Depends(require_admin),
):
    """跨用户查询文件列表（可按用户名筛选）"""
    files = await get_all_files(limit=limit, offset=offset, username=username)
    return {
        "success": True,
        "total": len(files),
        "files": [
            {
                "id": f["id"],
                "owner_id": f["owner_id"],
                "username": f.get("username", ""),
                "filename": f["filename"],
                "file_size": f.get("file_size", 0),
                "chunk_count": f.get("chunk_count", 0),
                "created_at": f["created_at"],
            }
            for f in files
        ],
    }


# ═══════════════════════════════════════════
# 审计日志查询
# ═══════════════════════════════════════════

@router.get("/audit-logs")
async def get_audit_logs(
    limit: int = Query(50, ge=1, le=500),
    action: str = None,
    username: str = None,
    current_user: dict = Depends(require_admin),
):
    """查询审计日志（管理员）"""
    rows = await db_get_audit_logs(limit=limit, action=action, username=username)
    return {
        "success": True,
        "total": len(rows),
        "logs": rows,
    }


# ═══════════════════════════════════════════
# 熔断器状态
# ═══════════════════════════════════════════

@router.get("/circuit-status")
async def get_circuit_status(current_user: dict = Depends(require_admin)):
    """查看所有熔断器状态"""
    return {
        "success": True,
        "circuits": [
            {
                "name": llm_circuit_breaker.name,
                "state": llm_circuit_breaker.state.value,
                "failure_count": llm_circuit_breaker._failure_count,
                "can_pass": llm_circuit_breaker.can_pass(),
            },
            {
                "name": embedding_circuit_breaker.name,
                "state": embedding_circuit_breaker.state.value,
                "failure_count": embedding_circuit_breaker._failure_count,
                "can_pass": embedding_circuit_breaker.can_pass(),
            },
        ],
    }


# ═══════════════════════════════════════════
# 异步队列状态
# ═══════════════════════════════════════════

@router.get("/queue-status")
async def get_queue_status(current_user: dict = Depends(require_admin)):
    """查看异步任务队列状态"""
    return {
        "success": True,
        "queue_size": async_queue.get_queue_size(),
        "backend": async_queue.backend_name(),   # "thread_pool" | "rq"
        "healthy": async_queue.health_check(),
    }


# ═══════════════════════════════════════════
# 会话统计
# ═══════════════════════════════════════════

@router.get("/session-stats")
async def get_session_stats(current_user: dict = Depends(require_admin)):
    """会话统计（需 Redis 连接）"""
    from core.session_manager import session_manager
    return {
        "success": True,
        "mode": "Redis" if session_manager._redis else "内存",
        "fallback_size": len(session_manager._fallback) if not session_manager._redis else 0,
    }
