"""
core/db/engine.py —— 引擎/会话生命周期、PRAGMA 调优、跨线程跑协程、建表与迁移、备份。
================================================

由 core/database.py 拆分而来（2026-09；拆分前是单文件 1366 行 / 57 个函数）。
数据库层只有 `core.db` 这一个入口，没有兼容门面 —— 直接从这里导入。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from loguru import logger
from sqlalchemy import (
    event,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from core.db.base import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    Base,
    get_db_path,
)


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
        "config_json": "TEXT",
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
