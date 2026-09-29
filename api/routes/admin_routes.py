"""
管理员运维接口 — 用户管理 / 仪表盘 / 审计日志 / 熔断状态 / 队列状态
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from core.async_queue import async_queue
from core.auth import require_admin
from core.circuit_breaker import embedding_circuit_breaker, llm_circuit_breaker
from core.database import (
    delete_user_cascade,
    get_all_chat_logs,
    get_all_files,
    get_files_by_owner,
    get_global_stats,
    get_user_by_id,
    get_users_with_counts,
    update_user_role,
)
from core.database import (
    # 别名：本模块的路由函数也叫 get_audit_logs，直接同名导入会被它覆盖
    get_audit_logs as db_get_audit_logs,
)
from core.paths import remove_within
from core.schemas import (
    DashboardStats,
    UserRoleUpdate,
)

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
    username: str | None = None,
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
    username: str | None = None,
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
    action: str | None = None,
    username: str | None = None,
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
    """查看所有熔断器状态（现在这些熔断器是真的接在调用链上的）"""
    return {
        "success": True,
        "circuits": [
            llm_circuit_breaker.snapshot(),
            embedding_circuit_breaker.snapshot(),
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
        "backend": async_queue.backend_name(),  # "thread_pool" | "rq"
        "healthy": async_queue.health_check(),
    }


# ═══════════════════════════════════════════
# 知识库一致性
# ═══════════════════════════════════════════


@router.get("/kb-consistency")
async def get_kb_consistency(current_user: dict = Depends(require_admin)):
    """
    检查「文件表」与「向量库」是否一致。

    这两处存储会独立漂移，而且**漂移时不报错** —— 表现为界面显示
    「知识库为空」，但模型仍能引用某份文档回答，用户看到的是一个自信的
    错答案。本项目真实踩过一次：清理测试数据时删了数据库行与磁盘文件，
    漏了向量库，于是"空知识库"照样答出了测试夹具里的内容。
    """
    from core.kb_tasks import check_kb_consistency

    report = await asyncio.to_thread(check_kb_consistency, 1)
    return {"success": True, **report}


@router.post("/kb-consistency/repair")
async def repair_kb_consistency_endpoint(current_user: dict = Depends(require_admin)):
    """
    清理孤儿向量块（向量库里有、文件表里没有的那些）。

    只清这一类 —— 它们是「空知识库仍能回答」的根因。
    「文件表有、向量库没有」不动：那类重新入库即可恢复，
    自动删除会丢掉书目记录。
    """
    from core.kb_tasks import repair_kb_consistency

    result = await asyncio.to_thread(repair_kb_consistency, 1)
    logger.info(
        "[Admin] 知识库一致性修复: {} 清除了 {} 个孤儿块",
        current_user["username"],
        result["removed_chunks"],
    )
    return {"success": True, **result}


# 说明：原有一个 /session-stats 接口，返回 core.session_manager 的状态。
# 该模块是一个从未被调用的会话存储（create_session/add_message/get_session
# 零调用），会话上下文实际由 chat_logs + conversation_id 承担，
# 因此连模块带接口一并移除，避免"日志里声称有 Redis 会话管理、实际什么也没做"。
