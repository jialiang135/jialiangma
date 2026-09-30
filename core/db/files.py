"""
core/db/files.py —— 知识库文件记录 + 上传任务进度。
================================================

由 core/database.py 拆分而来（2026-09；拆分前是单文件 1366 行 / 57 个函数）。
数据库层只有 `core.db` 这一个入口，没有兼容门面 —— 直接从这里导入。
"""

from __future__ import annotations

from sqlalchemy import (
    delete,
    select,
    update,
)

from core.db.base import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    utcnow,
)
from core.db.engine import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    session_scope,
)
from core.db.models import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    FileRecord,
    UploadTask,
    User,
)


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
