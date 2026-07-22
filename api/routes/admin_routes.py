"""
管理员运维接口 — 查看审计日志、熔断状态、队列状态
"""
import sqlite3
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from core.auth import require_admin
from core.circuit_breaker import llm_circuit_breaker, embedding_circuit_breaker
from core.async_queue import async_queue

PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
DB_PATH = PROJECT_ROOT / "assets" / "personal_agent.db"

router = APIRouter(prefix="/api/admin", tags=["运维"])


# ─── 审计日志查询 ───
@router.get("/audit-logs")
async def get_audit_logs(
    limit: int = Query(50, ge=1, le=500),
    action: str = None,
    username: str = None,
    current_user: dict = Depends(require_admin),
):
    """查询审计日志（管理员）"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    query = "SELECT * FROM audit_log WHERE 1=1"
    params = []
    if action:
        query += " AND action LIKE ?"
        params.append(f"%{action}%")
    if username:
        query += " AND username = ?"
        params.append(username)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return {
        "success": True,
        "total": len(rows),
        "logs": [dict(r) for r in rows],
    }


# ─── 熔断器状态 ───
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


# ─── 异步队列状态 ───
@router.get("/queue-status")
async def get_queue_status(current_user: dict = Depends(require_admin)):
    """查看异步任务队列状态"""
    return {
        "success": True,
        "queue_size": async_queue.get_queue_size(),
        "redis_available": async_queue._rq_queue is not None,
    }


# ─── 会话统计 ───
@router.get("/session-stats")
async def get_session_stats(current_user: dict = Depends(require_admin)):
    """会话统计（需 Redis 连接）"""
    from core.session_manager import session_manager
    return {
        "success": True,
        "mode": "Redis" if session_manager._redis else "内存",
        "fallback_size": len(session_manager._fallback) if not session_manager._redis else 0,
    }
