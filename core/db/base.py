"""
core/db/base.py —— ORM 基类、UTC 时间类型、主键时间戳等最底层的东西。
================================================

由 core/database.py 拆分而来（2026-09；拆分前是单文件 1366 行 / 57 个函数）。
数据库层只有 `core.db` 这一个入口，没有兼容门面 —— 直接从这里导入。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import (
    String,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from config.settings import settings

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
