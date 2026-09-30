"""
core/db/stats.py —— 跨实体的聚合统计（管理后台仪表盘用）。
================================================

由 core/database.py 拆分而来（2026-09；拆分前是单文件 1366 行 / 57 个函数）。
数据库层只有 `core.db` 这一个入口，没有兼容门面 —— 直接从这里导入。
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

from sqlalchemy import (
    func,
    select,
)

from config.settings import settings
from core.db.engine import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    session_scope,
)
from core.db.models import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    ChatLog,
    FileRecord,
    TokenUsage,
    User,
)


async def get_global_stats() -> dict:
    """全局仪表盘数据（管理员用）。"""
    async with session_scope() as session:
        user_count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        file_count = (
            await session.execute(select(func.count()).select_from(FileRecord))
        ).scalar_one()
        # date('now') 是 SQLite 的 UTC 日期，与库中 UTC 写入一致
        today_chats = (
            await session.execute(
                select(func.count())
                .select_from(ChatLog)
                .where(func.date(ChatLog.created_at) == func.date("now"))
            )
        ).scalar_one()
        total_chats = (
            await session.execute(select(func.count()).select_from(ChatLog))
        ).scalar_one()
        total_tokens = (
            await session.execute(
                select(
                    func.coalesce(
                        func.sum(TokenUsage.prompt_tokens + TokenUsage.completion_tokens), 0
                    )
                )
            )
        ).scalar_one()
        total_cost = (
            await session.execute(select(func.coalesce(func.sum(TokenUsage.cost_estimate), 0.0)))
        ).scalar_one()

    # 磁盘用量（同步 IO，但这个函数是低频的管理接口调用）
    return {
        "user_count": user_count,
        "file_count": file_count,
        "today_chats": today_chats,
        "total_chats": total_chats,
        "total_tokens": total_tokens,
        "total_cost": round(float(total_cost or 0), 6),
        "disk_used_mb": _dir_size_mb(settings.resolve_path(settings.upload_dir)),
        "chroma_db_mb": _dir_size_mb(settings.resolve_path(settings.chroma_persist_dir)),
    }


def _dir_size_mb(path: Path) -> float:
    total = 0
    if not os.path.exists(path):
        return 0.0
    for root, _dirs, files in os.walk(path):
        for f in files:
            with suppress(OSError):
                total += os.path.getsize(os.path.join(root, f))
    return round(total / (1024 * 1024), 2)


# ============================================================
# 审计日志
# ============================================================
