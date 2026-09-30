"""
core/db/users.py —— 用户与登录：注册、查询、角色、登录失败锁定、级联删除。
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
    update,
)

from config.settings import settings
from core.db.base import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    utcnow,
)
from core.db.engine import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    session_scope,
)
from core.db.models import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    AuditLog,
    ChatLog,
    FileRecord,
    LoginAttempt,
    TokenUsage,
    UploadTask,
    User,
)


async def create_admin_user(username: str, password_hash: str) -> int:
    """创建管理员用户（已存在则确保其角色为 admin），返回用户 id。"""
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()

        if user is not None:
            if user.role != "admin":
                user.role = "admin"
            logger.info("管理员账号已存在，跳过创建")
            return user.id

        user = User(username=username, password_hash=password_hash, role="admin")
        session.add(user)
        await session.flush()  # 必须在 flush 后取 id，否则是 None
        logger.info("管理员账号创建成功: {} (id={}, role=admin)", username, user.id)
        return user.id


async def create_user(username: str, password_hash: str, role: str = "user") -> int:
    """创建普通用户，返回用户 id。用户名已存在则抛 ValueError。"""
    async with session_scope() as session:
        existing = (
            await session.execute(select(User.id).where(User.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            raise ValueError(f"用户名已被占用: {username}")

        user = User(username=username, password_hash=password_hash, role=role)
        session.add(user)
        await session.flush()
        logger.info("用户创建成功: {} (id={}, role={})", username, user.id, role)
        return user.id


def _user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "password_hash": user.password_hash,
        "role": user.role,
        "created_at": user.created_at,
    }


async def get_user_by_username(username: str) -> dict | None:
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        return _user_to_dict(user) if user else None


async def get_user_by_id(user_id: int) -> dict | None:
    async with session_scope() as session:
        user = await session.get(User, user_id)
        return _user_to_dict(user) if user else None


# ============================================================
# 文件元数据
# ============================================================


async def check_login_locked(username: str) -> bool:
    """
    检查用户是否因连续登录失败被锁定。

    注意：库中 created_at 是 TEXT（UTC，'YYYY-MM-DD HH:MM:SS'）。
    原实现拿 ``cutoff.isoformat()`` 去比（带 'T'），而 ASCII 里
    ``'T' > ' '``，导致**同一天的记录全被误判为"晚于起点"**，
    锁定窗口实际失效。改用同一格式绑定即可。
    """
    cutoff = utcnow() - timedelta(minutes=settings.login_lockout_minutes)
    async with session_scope() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(LoginAttempt)
                .where(
                    LoginAttempt.username == username,
                    LoginAttempt.success == 0,
                    LoginAttempt.created_at > cutoff,
                )
            )
        ).scalar_one()
        return count >= settings.max_login_attempts


async def record_login_attempt(
    username: str, ip_address: str | None = None, success: bool = False
) -> None:
    async with session_scope() as session:
        session.add(
            LoginAttempt(username=username, ip_address=ip_address, success=1 if success else 0)
        )


async def count_recent_failed_logins(username: str) -> int:
    """最近锁定窗口内的失败次数（供管理接口/诊断使用）。"""
    cutoff = utcnow() - timedelta(minutes=settings.login_lockout_minutes)
    async with session_scope() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(LoginAttempt)
                .where(
                    LoginAttempt.username == username,
                    LoginAttempt.success == 0,
                    LoginAttempt.created_at > cutoff,
                )
            )
        ).scalar_one()


# ============================================================
# 管理员：用户管理
# ============================================================


async def get_all_users() -> list[dict]:
    async with session_scope() as session:
        rows = (
            (await session.execute(select(User).order_by(User.created_at.desc()))).scalars().all()
        )
        return [
            {"id": u.id, "username": u.username, "role": u.role, "created_at": u.created_at}
            for u in rows
        ]


async def get_users_with_counts() -> list[dict]:
    """
    用户列表 + 每人的文件数/对话数。

    原实现是 N+1：先查用户，再对每个用户各开两次连接统计。这里改成两条
    分组聚合再在内存里合并，用户数增长时不会线性放大查询次数。
    """
    async with session_scope() as session:
        users = (
            (await session.execute(select(User).order_by(User.created_at.desc()))).scalars().all()
        )

        file_counts = dict(
            (
                await session.execute(
                    select(FileRecord.owner_id, func.count()).group_by(FileRecord.owner_id)
                )
            ).all()
        )
        chat_counts = dict(
            (
                await session.execute(
                    select(ChatLog.owner_id, func.count()).group_by(ChatLog.owner_id)
                )
            ).all()
        )

    return [
        {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "created_at": u.created_at,
            "file_count": file_counts.get(u.id, 0),
            "chat_count": chat_counts.get(u.id, 0),
        }
        for u in users
    ]


async def update_user_role(user_id: int, role: str) -> bool:
    if role not in ("admin", "user"):
        raise ValueError("角色只能是 admin 或 user")
    async with session_scope() as session:
        result = await session.execute(update(User).where(User.id == user_id).values(role=role))
        return (result.rowcount or 0) > 0


async def delete_user_cascade(user_id: int) -> dict:
    """
    删除用户及其关联数据，返回各表删除条数。

    显式逐表删除（而不是 f-string 拼表名循环）：
    拼表名的写法虽然表名是硬编码、当前无注入风险，但一旦有人把它参数化
    就会变成注入点，不值得留这个模式。
    """
    counts: dict[str, int] = {}
    async with session_scope() as session:
        for name, model in (
            ("chat_logs", ChatLog),
            ("token_usage", TokenUsage),
            ("login_attempts", None),  # 该表按 username 关联，见下
            ("files", FileRecord),
            ("upload_tasks", UploadTask),
        ):
            if model is None:
                continue
            result = await session.execute(delete(model).where(model.owner_id == user_id))
            counts[name] = result.rowcount or 0

        # audit_log 按 user_id
        result = await session.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        counts["audit_log"] = result.rowcount or 0

        result = await session.execute(delete(User).where(User.id == user_id))
        counts["users"] = result.rowcount or 0
    return counts


# ============================================================
# 管理员：全局查询
# ============================================================
