"""
core/db/chunk_config.py —— 知识库级切块配置的持久化。
================================================

一张表一行（``owner_id`` 主键），存 mode / chunk_size / chunk_overlap 与可选的
自定义分隔符。这里只管读写，**校验与默认值在 ``core/chunking.py``**（那里是
切块配置的唯一真相源，模型上的 server_default 只是裸 SQL 插行时的兜底）。

由 ``core/db/__init__.py`` 统一导出，与其它实体模块同款。
"""

from __future__ import annotations

import json

from core.db.base import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    utcnow,
)
from core.db.engine import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    session_scope,
)
from core.db.models import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    KbChunkConfig,
)


def _encode_separators(separators: list[str] | None) -> str | None:
    """列表 → JSON 字符串；空列表与 None 都存 NULL（语义等价：用内置默认）。"""
    if not separators:
        return None
    return json.dumps(list(separators), ensure_ascii=False)


def _decode_separators(raw: str | None) -> list[str] | None:
    """
    还原分隔符列表。

    存量/被手改的脏值不该让整个读取失败：解析不出来就当作"没有自定义分隔符"
    （回落内置默认），而不是抛异常把入库链路卡住。
    """
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return value or None
    return None


def _row_to_dict(record: KbChunkConfig) -> dict:
    return {
        "owner_id": record.owner_id,
        "mode": record.mode,
        "chunk_size": record.chunk_size,
        "chunk_overlap": record.chunk_overlap,
        "separators": _decode_separators(record.separators_json),
        "updated_at": record.updated_at,
    }


async def get_chunk_config(owner_id: int) -> dict | None:
    """读取某知识库的切块配置；从未配置过时返回 ``None``（由上层回落默认）。"""
    async with session_scope() as session:
        record = await session.get(KbChunkConfig, owner_id)
        return _row_to_dict(record) if record else None


async def upsert_chunk_config(
    owner_id: int,
    mode: str,
    chunk_size: int,
    chunk_overlap: int,
    separators: list[str] | None = None,
) -> None:
    """
    写入（无则新建）某知识库的切块配置。

    用"先查再改"而不是方言相关的 ``INSERT ... ON CONFLICT``：这里只服务 SQLite
    单文件库，两行代码足够，换来的是不依赖具体方言、测试里用假会话也能跑。
    """
    async with session_scope() as session:
        record = await session.get(KbChunkConfig, owner_id)
        values = {
            "mode": mode,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "separators_json": _encode_separators(separators),
            "updated_at": utcnow(),
        }
        if record is None:
            session.add(KbChunkConfig(owner_id=owner_id, **values))
        else:
            for key, value in values.items():
                setattr(record, key, value)
