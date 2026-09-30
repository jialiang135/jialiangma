"""
core/db/chats.py —— 对话记录与轮次分组（conversation）。
================================================

由 core/database.py 拆分而来（2026-09；拆分前是单文件 1366 行 / 57 个函数）。
数据库层只有 `core.db` 这一个入口，没有兼容门面 —— 直接从这里导入。
"""

from __future__ import annotations

from sqlalchemy import (
    delete,
    func,
    select,
    text,
)

from core.db.engine import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    session_scope,
)
from core.db.models import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    ChatLog,
    User,
)


def _chat_to_dict(log: ChatLog) -> dict:
    return {
        "id": log.id,
        "owner_id": log.owner_id,
        "agent_mode": log.agent_mode,
        "conversation_id": log.conversation_id,
        "question": log.question,
        "answer": log.answer,
        "reasoning": log.reasoning,
        "sources": log.sources,
        "created_at": log.created_at,
    }


async def insert_chat_log(
    owner_id: int,
    agent_mode: str,
    question: str,
    answer: str,
    reasoning: str | None = None,
    sources: str | None = None,
    conversation_id: str | None = None,
) -> int:
    async with session_scope() as session:
        log = ChatLog(
            owner_id=owner_id,
            agent_mode=agent_mode,
            conversation_id=conversation_id,
            question=question,
            answer=answer,
            reasoning=reasoning,
            sources=sources,
        )
        session.add(log)
        await session.flush()
        return log.id


async def get_chat_history(owner_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(ChatLog)
                    .where(ChatLog.owner_id == owner_id)
                    .order_by(ChatLog.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return [_chat_to_dict(r) for r in rows]


async def get_chat_history_count(owner_id: int) -> int:
    async with session_scope() as session:
        return (
            await session.execute(
                select(func.count()).select_from(ChatLog).where(ChatLog.owner_id == owner_id)
            )
        ).scalar_one()


async def get_conversations(owner_id: int, limit: int = 30) -> list[dict]:
    """
    对话组摘要（按 conversation_id 分组；旧数据 conversation_id 为 NULL 时
    以 ``__single_<id>`` 视为独立对话）。

    这条聚合带相关子查询与字符串拼接，写成 ORM 表达力不足且可读性差，
    因此保留原生 SQL（用绑定参数，无注入风险）。
    """
    stmt = text("""
        SELECT
            COALESCE(conversation_id, '__single_' || id) AS group_id,
            MIN(id) AS first_log_id,
            COUNT(*) AS turn_count,
            MAX(created_at) AS last_at,
            (SELECT question FROM chat_logs cl2
             WHERE cl2.owner_id = :owner_id
               AND COALESCE(cl2.conversation_id, '__single_' || cl2.id)
                   = COALESCE(cl.conversation_id, '__single_' || cl.id)
             ORDER BY cl2.created_at ASC LIMIT 1
            ) AS first_question
        FROM chat_logs cl
        WHERE cl.owner_id = :owner_id
        GROUP BY COALESCE(cl.conversation_id, '__single_' || cl.id)
        ORDER BY MAX(cl.created_at) DESC
        LIMIT :limit
    """)
    async with session_scope() as session:
        rows = (
            (await session.execute(stmt, {"owner_id": owner_id, "limit": limit})).mappings().all()
        )
        return [dict(r) for r in rows]


async def get_chat_by_conversation_id(owner_id: int, conversation_id: str) -> list[dict]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(ChatLog)
                    .where(ChatLog.owner_id == owner_id, ChatLog.conversation_id == conversation_id)
                    .order_by(ChatLog.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        return [_chat_to_dict(r) for r in rows]


async def get_chat_log_by_id(owner_id: int, log_id: int) -> dict | None:
    """按 id 取单条（供旧格式 ``__single_<id>`` 的历史会话使用）。"""
    async with session_scope() as session:
        log = (
            await session.execute(
                select(ChatLog).where(ChatLog.id == log_id, ChatLog.owner_id == owner_id)
            )
        ).scalar_one_or_none()
        return _chat_to_dict(log) if log else None


async def delete_conversation(owner_id: int, group_id: str) -> int:
    """
    删除整个对话组。
    - ``__single_<id>`` → 只删该单条
    - 其他（UUID）→ 删除该 conversation_id 的全部记录
    """
    async with session_scope() as session:
        if group_id.startswith("__single_"):
            log_id = int(group_id.replace("__single_", ""))
            result = await session.execute(
                delete(ChatLog).where(ChatLog.id == log_id, ChatLog.owner_id == owner_id)
            )
        else:
            result = await session.execute(
                delete(ChatLog).where(
                    ChatLog.conversation_id == group_id, ChatLog.owner_id == owner_id
                )
            )
        return result.rowcount or 0


# ============================================================
# 登录尝试
# ============================================================


async def get_all_chat_logs(
    limit: int = 50, offset: int = 0, username: str | None = None
) -> list[dict]:
    async with session_scope() as session:
        stmt = select(ChatLog, User.username).join(User, ChatLog.owner_id == User.id)
        if username:
            stmt = stmt.where(User.username == username)
        rows = (
            await session.execute(
                stmt.order_by(ChatLog.created_at.desc()).limit(limit).offset(offset)
            )
        ).all()
        out = []
        for log, uname in rows:
            item = _chat_to_dict(log)
            item["username"] = uname
            out.append(item)
        return out
