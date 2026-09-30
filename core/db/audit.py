"""
core/db/audit.py —— 审计日志的写入、查询与保留期清理。
================================================

由 core/database.py 拆分而来（2026-09；拆分前是单文件 1366 行 / 57 个函数）。
数据库层只有 `core.db` 这一个入口，没有兼容门面 —— 直接从这里导入。
"""

from __future__ import annotations

from datetime import timedelta

from loguru import logger
from sqlalchemy import (
    delete,
    func,
    select,
)

from core.db.base import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    utcnow,
)
from core.db.engine import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    session_scope,
)
from core.db.models import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    AuditLog,
)


async def insert_audit_log(
    action: str,
    user_id: int | None = None,
    username: str | None = None,
    resource: str | None = None,
    detail: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    duration_ms: float | None = None,
    status: str = "success",
) -> None:
    """
    写入一条审计日志。

    审计是"每个请求都写"的高频路径，用独立 session_scope：
    失败不能影响主请求，因此这里吞掉异常并记 warning。
    """
    try:
        async with session_scope() as session:
            session.add(
                AuditLog(
                    user_id=user_id,
                    username=username,
                    action=action,
                    resource=resource,
                    detail=detail,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    duration_ms=duration_ms,
                    status=status,
                )
            )
    except Exception as e:
        logger.warning("审计日志写入失败: {}", e)


async def get_audit_logs(
    limit: int = 50, offset: int = 0, action: str | None = None, username: str | None = None
) -> list[dict]:
    """查询审计日志，支持按 action 模糊匹配、按 username 精确过滤。"""
    async with session_scope() as session:
        stmt = select(AuditLog)
        if action:
            stmt = stmt.where(AuditLog.action.like(f"%{action}%"))
        if username:
            stmt = stmt.where(AuditLog.username == username)
        rows = (
            (
                await session.execute(
                    stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "user_id": r.user_id,
                "username": r.username,
                "action": r.action,
                "resource": r.resource,
                "detail": r.detail,
                "ip_address": r.ip_address,
                "user_agent": r.user_agent,
                "duration_ms": r.duration_ms,
                "status": r.status,
                "created_at": r.created_at,
            }
            for r in rows
        ]


async def cleanup_old_audit_logs(retention_days: int) -> int:
    """删除超过保留期的审计日志，返回删除条数。"""
    cutoff = utcnow() - timedelta(days=retention_days)
    async with session_scope() as session:
        result = await session.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
        return result.rowcount or 0


async def count_audit_logs() -> int:
    async with session_scope() as session:
        return (await session.execute(select(func.count()).select_from(AuditLog))).scalar_one()


# ============================================================
# 评测报告
# ============================================================
