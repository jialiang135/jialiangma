"""
数据库层 —— SQLAlchemy 2.0 async + aiosqlite
=============================================

为什么改成 async
----------------
原实现是同步 ``sqlite3``，每个函数各自 ``connect()``，却在 FastAPI 的
``async def`` 端点里被直接调用 —— **一次查询就阻塞整个事件循环**，
同进程内的所有请求一起排队。换成 aiosqlite 后 DB 操作真正让出控制权。

为什么单独处理 PRAGMA
---------------------
原来全仓库没有任何 PRAGMA，实测 ``journal_mode=delete`` 且无 ``busy_timeout``：
任意两个写者相撞会**立刻**抛 ``database is locked``（是报错，不是等待）。
本服务至少有三个写者：事件循环、异步任务线程池、APScheduler 后台线程。
因此连接建立时统一设置 ``WAL`` + ``busy_timeout`` + ``synchronous=NORMAL``。

关于时区（容易踩）
------------------
SQLite 的 ``CURRENT_TIMESTAMP`` 返回 **UTC**，库里既有数据也是 UTC
（实测 ``'2026-09-28 07:36:52'`` 对应北京时间 15:36）。
因此本项目**所有时间一律按 UTC 写入**。混入本地时间会让登录锁定窗口和
"今日统计"整体错 8 小时。UTCDateTime 类型负责格式与时区的统一。

关于外键
--------
DDL 里保留了 ``FOREIGN KEY`` 声明，但**运行时不启用强制**
（不设置 ``PRAGMA foreign_keys=ON``）。原因：匿名对话接口
（``/api/chat/stream/public``）以 ``owner_id=0`` 落库，而 users 表里没有
id=0 的用户 —— 一旦启用强制，匿名对话会全部写入失败。
这是既有设计的取舍，此处显式记录，避免后来者误以为是遗漏。
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import (
    Float,
    Integer,
    String,
    Text,
    delete,
    event,
    func,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool
from sqlalchemy.types import TypeDecorator

from config.settings import settings

# SQLite 既有数据的时间格式（由 CURRENT_TIMESTAMP 写入，UTC）
_SQLITE_DT_FMT = "%Y-%m-%d %H:%M:%S"


def get_db_path() -> Path:
    """
    数据库文件路径。

    从 settings 读取而非硬编码，测试才能把它指向临时目录
    （原实现硬编码，导致跑测试会污染真实库）。
    """
    return settings.resolve_path(settings.db_path)


# ============================================================
# 类型
# ============================================================


class UTCDateTime(TypeDecorator):
    """
    以 ``'YYYY-MM-DD HH:MM:SS'``（UTC）存储的 datetime。

    为什么不直接用 ``DateTime``：库中既有 ``created_at`` 由 SQLite 的
    ``CURRENT_TIMESTAMP`` 写成，是 TEXT ``'2026-07-21 13:41:28'``。若模型声明
    ``DateTime``，SQLAlchemy 写入时会带微秒（``...28.123456``），与存量行格式
    不一致，做字符串比较和 ``date()`` 聚合时会出现难以察觉的偏差。
    这里显式统一格式，读回时再解析成 datetime 交给上层。
    """

    impl = String(19)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, datetime):
            # 统一按 UTC 写入，与 CURRENT_TIMESTAMP 语义一致
            if value.tzinfo is not None:
                value = value.astimezone(UTC).replace(tzinfo=None)
            return value.strftime(_SQLITE_DT_FMT)
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.strptime(str(value)[:19], _SQLITE_DT_FMT)
        except ValueError:
            logger.warning("无法解析时间字段: {!r}", value)
            return None


def utcnow() -> datetime:
    """当前 UTC 时间（naive，与库中格式对齐）。"""
    return datetime.now(UTC).replace(tzinfo=None)


# ============================================================
# 模型
# ============================================================


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'user'"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class FileRecord(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    filepath: Mapped[str] = mapped_column(String, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    chunk_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    file_hash: Mapped[str] = mapped_column(String, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class UploadTask(Base):
    __tablename__ = "upload_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'pending'"))
    progress: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    chunk_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class ChatLog(Base):
    __tablename__ = "chat_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_mode: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'chat'"))
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    # reasoning / sources 都是 JSON 字符串塞在 TEXT 里。
    # 刻意**不**改成 JSON 列：改了存量数据读不出来。
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    sources: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class TokenUsage(Base):
    __tablename__ = "token_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    reasoning_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cached_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_estimate: Mapped[float] = mapped_column(Float, server_default=text("0.0"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String, nullable=True)
    success: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class AuditLog(Base):
    """
    审计日志。

    原先建表语句写在 core/audit.py 里，导致 schema 分散在两个模块。
    这里收编，让"表结构的唯一来源"是这个文件。
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    resource: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, server_default=text("'success'"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class EvalReport(Base):
    """
    评测报告。

    这张表**原本是孤儿**：库里有、但全仓库没有任何代码创建或读写它
    （eval 功能被删除了一半，schema 留了下来）。这里接着它原来的字段设计
    补全实现，并加上异步任务需要的状态字段。

    指标分两类：
    - RAGAS（faithfulness / answer_relevancy / context_precision / context_recall）
      存 metrics_json
    - 自建的"诚实度"——知识库外问题是否如实说不知道，存 honesty_rate
    """

    __tablename__ = "eval_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    testset_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, server_default=text("'pending'"))
    progress: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    total_questions: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    completed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    answered_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    accuracy: Mapped[float] = mapped_column(Float, server_default=text("0.0"))
    hallucination_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    hallucination_rate: Mapped[float] = mapped_column(Float, server_default=text("0.0"))
    honesty_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    poor_retrieval_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    results_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendations_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


# ============================================================
# 引擎与会话
# ============================================================


def _build_engine():
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # SQLite 用 NullPool：文件本地访问，连接开销极低（微秒级），
    # 连接池主要收益是省掉 TCP 握手，对 SQLite 不适用。
    # 而池化会带来两类真实麻烦：
    #   1. 连接绑定在创建它的 event loop 上，测试里每个 asyncio.run 都是新 loop，
    #      复用池中旧连接会报 "attached to a different loop"；
    #   2. SSE 流这类长生命周期场景会长期占住一条连接。
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        future=True,
        poolclass=NullPool,
    )

    @event.listens_for(eng.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        """
        每个新连接都设置 PRAGMA。

        - WAL：读写并发不再互相阻塞（原 ``delete`` 模式下写会阻塞读）
        - busy_timeout：写锁冲突时等待而不是立刻报错（原来没有，直接
          ``database is locked``）
        - synchronous=NORMAL：WAL 下的推荐值，兼顾安全与性能
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    return eng


engine = _build_engine()

async_session_factory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """
    独立会话上下文，供**拿不到 FastAPI 依赖注入**的地方使用：

    - SSE 异步生成器（生命周期远长于请求处理）
    - LangGraph 节点（由 graph 内部调用）
    - 审计中间件（BaseHTTPMiddleware.dispatch）
    - 后台线程池任务与 APScheduler 线程

    用法::

        async with session_scope() as session:
            ...
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入用的会话。"""
    async with session_scope() as session:
        yield session


async def dispose_engine() -> None:
    """应用关闭时释放连接池。"""
    await engine.dispose()


# ------------------------------------------------------------
# 跨线程执行（供 APScheduler 后台线程使用）
# ------------------------------------------------------------

_main_loop: asyncio.AbstractEventLoop | None = None


def bind_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """应用启动时记录主事件循环，供后台线程提交协程。"""
    global _main_loop
    _main_loop = loop


def run_async_from_thread(coro, timeout: float = 30.0):
    """
    在**非事件循环线程**里执行异步 DB 操作（APScheduler 的 BackgroundScheduler
    就在自己的线程里跑，那里没有 event loop 可 await）。

    不能直接用 ``asyncio.run`` 或 ``asyncio.new_event_loop``：那会新建一个循环，
    而 aiosqlite 的连接与其创建时的循环绑定，跨循环使用会出问题。
    这里把协程提交到主循环执行并等待结果。
    """
    if _main_loop is None or not _main_loop.is_running():
        raise RuntimeError("主事件循环未就绪，无法跨线程执行异步数据库操作")
    future = asyncio.run_coroutine_threadsafe(coro, _main_loop)
    return future.result(timeout=timeout)


def run_async_blocking(coro, timeout: float = 30.0):
    """
    在**新线程 + 新事件循环**里把协程跑完，并阻塞等待结果。

    用途只有一个：客户端断流时需要在 ``except asyncio.CancelledError`` 里落库。
    那一刻当前协程已被取消，**任何 await 都会立刻再抛 CancelledError**
    （且它继承自 BaseException，``except Exception`` 接不住），
    所以既不能 await，也不能用 ``run_async_from_thread``（那是提交回同一个
    已被取消的循环，会自锁）。

    新线程里新建循环 + NullPool 现开连接，二者自洽，不共享任何跨循环状态。
    代价是线程创建开销 —— 只在"用户点了停止"这一条罕见路径上付出，可接受。
    """
    result: dict = {}

    def _worker() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as e:
            result["error"] = e

    thread = threading.Thread(target=_worker, daemon=True, name="persist-on-cancel")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError("落库超时未完成")
    if "error" in result:
        raise result["error"]
    return result.get("value")


# ============================================================
# 初始化
# ============================================================

# 旧库缺失列时的补齐定义（新库由 Base.metadata.create_all 一次建好）
_LEGACY_COLUMNS = {
    "users": {"role": "TEXT NOT NULL DEFAULT 'admin'"},
    "files": {"file_hash": "TEXT DEFAULT ''"},
    "token_usage": {
        "reasoning_tokens": "INTEGER DEFAULT 0",
        "cached_tokens": "INTEGER DEFAULT 0",
    },
    # eval_reports 是历史遗留表，字段比现在需要的少，补齐任务状态与指标列
    "eval_reports": {
        "status": "TEXT DEFAULT 'pending'",
        "progress": "INTEGER DEFAULT 0",
        "answered_count": "INTEGER DEFAULT 0",
        "honesty_rate": "REAL",
        "duration_seconds": "REAL",
        "metrics_json": "TEXT",
        "error": "TEXT DEFAULT ''",
    },
}


async def init_database() -> None:
    """建表 + 补齐旧库缺失的列。"""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # 兼容旧库：create_all 不会修改已存在的表，缺列要显式补
        for table, columns in _LEGACY_COLUMNS.items():
            rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
            existing = {row[1] for row in rows}
            for col, ddl in columns.items():
                if col not in existing:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                    logger.info("数据库迁移：{} 表补充列 {}", table, col)

        # 索引（create_all 只建模型上声明的；这两条是历史遗留的手工索引）
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_login_attempts_user "
                "ON login_attempts(username, created_at)"
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)"))
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)")
        )
        # 对话历史按 (owner_id, created_at) 查询最频繁
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_chat_logs_owner_created "
                "ON chat_logs(owner_id, created_at)"
            )
        )

    logger.info("数据库初始化完成: {}", str(db_path))


# ============================================================
# 用户操作
# ============================================================


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


def _file_to_dict(record: FileRecord) -> dict:
    return {
        "id": record.id,
        "owner_id": record.owner_id,
        "filename": record.filename,
        "filepath": record.filepath,
        "file_size": record.file_size,
        "chunk_count": record.chunk_count,
        "file_hash": record.file_hash,
        "created_at": record.created_at,
    }


async def insert_file_record(
    owner_id: int, filename: str, filepath: str, file_size: int = 0, chunk_count: int = 0
) -> int:
    """插入文件记录，返回记录 id。"""
    async with session_scope() as session:
        record = FileRecord(
            owner_id=owner_id,
            filename=filename,
            filepath=filepath,
            file_size=file_size,
            chunk_count=chunk_count,
        )
        session.add(record)
        await session.flush()  # 调用方会立刻用这个 id 做后续更新
        return record.id


async def get_files_by_owner(owner_id: int) -> list[dict]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(FileRecord)
                    .where(FileRecord.owner_id == owner_id)
                    .order_by(FileRecord.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [_file_to_dict(r) for r in rows]


async def get_file_by_id(file_id: int) -> dict | None:
    async with session_scope() as session:
        record = await session.get(FileRecord, file_id)
        return _file_to_dict(record) if record else None


async def delete_file_record(file_id: int, owner_id: int) -> bool:
    async with session_scope() as session:
        result = await session.execute(
            delete(FileRecord).where(FileRecord.id == file_id, FileRecord.owner_id == owner_id)
        )
        return (result.rowcount or 0) > 0


async def delete_all_file_records(owner_id: int) -> int:
    async with session_scope() as session:
        result = await session.execute(delete(FileRecord).where(FileRecord.owner_id == owner_id))
        return result.rowcount or 0


async def update_file_chunk_count(file_id: int, chunk_count: int) -> None:
    async with session_scope() as session:
        await session.execute(
            update(FileRecord).where(FileRecord.id == file_id).values(chunk_count=chunk_count)
        )


async def update_file_hash(file_id: int, file_hash: str) -> None:
    """
    更新文件哈希。

    原实现由调用方自己开连接执行裸 SQL（`UPDATE files SET file_hash = ?`），
    这里收编成正式接口，避免调用点绕过数据层。
    """
    async with session_scope() as session:
        await session.execute(
            update(FileRecord).where(FileRecord.id == file_id).values(file_hash=file_hash)
        )


async def find_file_by_hash_or_name(owner_id: int, file_hash: str, filename: str) -> dict | None:
    """按 (哈希 或 文件名) 查重，用于上传去重。"""
    async with session_scope() as session:
        record = (
            await session.execute(
                select(FileRecord)
                .where(
                    FileRecord.owner_id == owner_id,
                    (FileRecord.file_hash == file_hash) | (FileRecord.filename == filename),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return _file_to_dict(record) if record else None


# ============================================================
# 上传任务
# ============================================================


async def create_upload_task(
    task_id: str, owner_id: int, filename: str, file_size: int = 0
) -> None:
    async with session_scope() as session:
        session.add(
            UploadTask(
                task_id=task_id,
                owner_id=owner_id,
                filename=filename,
                file_size=file_size,
                status="pending",
            )
        )


async def update_upload_task(task_id: str, **fields) -> None:
    """
    更新上传任务。允许的字段：status / progress / chunk_count / error。

    用白名单而非 **kwargs 直传：避免调用方拼错字段名却不报错。
    """
    allowed = {"status", "progress", "chunk_count", "error"}
    values = {k: v for k, v in fields.items() if k in allowed}
    if not values:
        return
    values["updated_at"] = utcnow()
    async with session_scope() as session:
        await session.execute(
            update(UploadTask).where(UploadTask.task_id == task_id).values(**values)
        )


async def get_upload_task(task_id: str, owner_id: int) -> dict | None:
    async with session_scope() as session:
        task = (
            await session.execute(
                select(UploadTask).where(
                    UploadTask.task_id == task_id, UploadTask.owner_id == owner_id
                )
            )
        ).scalar_one_or_none()
        if task is None:
            return None
        return {
            "task_id": task.task_id,
            "owner_id": task.owner_id,
            "filename": task.filename,
            "file_size": task.file_size,
            "status": task.status,
            "progress": task.progress,
            "chunk_count": task.chunk_count,
            "error": task.error,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }


# ============================================================
# 对话日志
# ============================================================


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


async def get_all_files(
    limit: int = 50, offset: int = 0, username: str | None = None
) -> list[dict]:
    async with session_scope() as session:
        stmt = select(FileRecord, User.username).join(User, FileRecord.owner_id == User.id)
        if username:
            stmt = stmt.where(User.username == username)
        rows = (
            await session.execute(
                stmt.order_by(FileRecord.created_at.desc()).limit(limit).offset(offset)
            )
        ).all()
        out = []
        for record, uname in rows:
            item = _file_to_dict(record)
            item["username"] = uname
            out.append(item)
        return out


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


def _eval_to_dict(report: EvalReport) -> dict:
    return {
        "id": report.id,
        "owner_id": report.owner_id,
        "testset_name": report.testset_name,
        "status": report.status,
        "progress": report.progress,
        "total_questions": report.total_questions,
        "completed": report.completed,
        "answered_count": report.answered_count,
        "hallucination_count": report.hallucination_count,
        "hallucination_rate": report.hallucination_rate,
        "honesty_rate": report.honesty_rate,
        "avg_match_score": report.avg_match_score,
        "poor_retrieval_count": report.poor_retrieval_count,
        "duration_seconds": report.duration_seconds,
        "metrics_json": report.metrics_json,
        "results_json": report.results_json,
        "recommendations_json": report.recommendations_json,
        "error": report.error,
        "created_at": report.created_at,
    }


# 允许通过 update_eval_report 更新的字段（白名单，防止拼错字段名却不报错）
_EVAL_UPDATABLE = {
    "status",
    "progress",
    "total_questions",
    "completed",
    "answered_count",
    "hallucination_count",
    "hallucination_rate",
    "honesty_rate",
    "avg_match_score",
    "poor_retrieval_count",
    "duration_seconds",
    "metrics_json",
    "results_json",
    "recommendations_json",
    "error",
}


async def create_eval_report(owner_id: int, testset_name: str, total_questions: int = 0) -> int:
    async with session_scope() as session:
        report = EvalReport(
            owner_id=owner_id,
            testset_name=testset_name,
            total_questions=total_questions,
            status="pending",
        )
        session.add(report)
        await session.flush()
        return report.id


async def update_eval_report(report_id: int, **fields) -> None:
    values = {k: v for k, v in fields.items() if k in _EVAL_UPDATABLE}
    if not values:
        return
    async with session_scope() as session:
        await session.execute(update(EvalReport).where(EvalReport.id == report_id).values(**values))


async def get_eval_report(report_id: int, owner_id: int | None = None) -> dict | None:
    async with session_scope() as session:
        stmt = select(EvalReport).where(EvalReport.id == report_id)
        if owner_id is not None:
            stmt = stmt.where(EvalReport.owner_id == owner_id)
        report = (await session.execute(stmt)).scalar_one_or_none()
        return _eval_to_dict(report) if report else None


async def list_eval_reports(owner_id: int | None = None, limit: int = 20) -> list[dict]:
    """列出评测报告（不含体积大的 results_json）。"""
    async with session_scope() as session:
        stmt = select(EvalReport)
        if owner_id is not None:
            stmt = stmt.where(EvalReport.owner_id == owner_id)
        rows = (
            (await session.execute(stmt.order_by(EvalReport.created_at.desc()).limit(limit)))
            .scalars()
            .all()
        )
        out = []
        for r in rows:
            item = _eval_to_dict(r)
            item.pop("results_json", None)  # 逐题明细只在详情接口返回
            out.append(item)
        return out


async def delete_eval_report(report_id: int) -> bool:
    """删除评测报告，返回是否删到了行。"""
    async with session_scope() as session:
        result = await session.execute(delete(EvalReport).where(EvalReport.id == report_id))
        return (result.rowcount or 0) > 0


# ============================================================
# 备份
# ============================================================


async def backup_database(dest_path: Path) -> None:
    """
    在线备份数据库。

    **不能用 shutil.copy2**：启用 WAL 后，主库文件里可能缺少尚未 checkpoint
    的事务，裸拷贝会得到一个不一致的副本。SQLite 官方做法是走 ``VACUUM INTO``
    （或 backup API），它能拿到一致快照且不需要额外连接管理。
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists():
        dest_path.unlink()
    async with engine.connect() as conn:
        # VACUUM INTO 的目标路径作为字面量传入（参数化不支持）
        await conn.execute(text(f"VACUUM INTO '{str(dest_path).replace(chr(39), chr(39) * 2)}'"))
    logger.info("数据库备份完成: {}", str(dest_path))
