"""
知识库管理路由（文件去重 + 异步处理 + 进度查询）
"""
import os
import shutil
import tempfile
import hashlib
import uuid
import sqlite3
from pathlib import Path
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from loguru import logger

from core.schemas import FileUploadResponse, KnowledgeBaseStats, FileMetaOut, APIResponse
from core.auth import get_current_user, require_admin
from core.database import (
    get_files_by_owner,
    get_file_by_id,
    delete_file_record,
    delete_all_file_records,
    insert_file_record,
    update_file_chunk_count,
)
from core.async_queue import async_queue
from config.settings import settings, PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "assets" / "personal_agent.db"

router = APIRouter(prefix="/api/kb", tags=["知识库"])


# ─── 辅助函数 ───

def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _compute_file_hash(filepath: str) -> str:
    """计算文件 MD5"""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_file_exists(owner_id: int, file_hash: str, filename: str) -> dict | None:
    """检查是否已有相同文件（hash 或 文件名 匹配）"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM files WHERE owner_id = ? AND (file_hash = ? OR filename = ?)",
        (owner_id, file_hash, filename),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _process_file_sync(file_path: str, filename: str, owner_id: int) -> dict:
    """同步处理单个文件：解析 → 分块 → 入库。返回结果字典。"""
    from rag.document_loader import load_documents_from_paths
    from rag.text_splitter import process_documents_batch
    from rag.vector_store import add_documents

    file_size = os.path.getsize(file_path)
    upload_dir = str(settings.resolve_path(settings.upload_dir))

    # 解析
    docs = load_documents_from_paths([file_path], upload_dir)
    if not docs or not docs[0].get("content", "").strip():
        return {"success": False, "error": "无法解析文件内容"}

    # 分块
    processed = process_documents_batch(docs)
    if not processed or not processed[0].get("chunks"):
        return {"success": False, "error": "内容为空"}

    chunks = processed[0]["chunks"]

    # 持久化
    dest_path = os.path.join(upload_dir, filename)
    shutil.copy2(file_path, dest_path)

    # 去重检查
    file_hash = _compute_file_hash(dest_path)
    existing = _check_file_exists(owner_id, file_hash, filename)
    if existing:
        os.unlink(dest_path)
        return {
            "success": True,
            "skipped": True,
            "message": f"文件已存在（# {existing['id']}），跳过重复上传",
            "existing_id": existing["id"],
        }

    # 向量化
    metadatas = [
        {"owner_id": owner_id, "source": filename, "chunk_idx": i, "filepath": dest_path}
        for i in range(len(chunks))
    ]
    add_documents(chunks, metadatas)

    # 数据库记录
    file_id = insert_file_record(
        owner_id=owner_id,
        filename=filename,
        filepath=dest_path,
        file_size=file_size,
        chunk_count=len(chunks),
    )
    update_file_chunk_count(file_id, len(chunks))
    # 更新 hash
    conn = _get_conn()
    conn.execute("UPDATE files SET file_hash = ? WHERE id = ?", (file_hash, file_id))
    conn.commit()
    conn.close()

    return {"success": True, "filename": filename, "chunks": len(chunks), "file_id": file_id}


def _process_file_async(task_id: str, tmp_path: str, filename: str, owner_id: int):
    """异步处理文件，更新 upload_tasks 表状态"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE upload_tasks SET status='processing', progress=30, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
            (task_id,),
        )
        conn.commit()
        result = _process_file_sync(tmp_path, filename, owner_id)
        if result.get("success"):
            if result.get("skipped"):
                conn.execute(
                    "UPDATE upload_tasks SET status='skipped', progress=100, error=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                    (result.get("message", "跳过"), task_id),
                )
            else:
                conn.execute(
                    "UPDATE upload_tasks SET status='done', progress=100, chunk_count=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                    (result.get("chunks", 0), task_id),
                )
        else:
            conn.execute(
                "UPDATE upload_tasks SET status='failed', progress=100, error=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                (result.get("error", "未知错误"), task_id),
            )
        conn.commit()
    except Exception as e:
        conn.execute(
            "UPDATE upload_tasks SET status='failed', progress=100, error=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
            (str(e)[:500], task_id),
        )
        conn.commit()
    finally:
        conn.close()
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ─── 路由 ───

@router.get("/files", response_model=KnowledgeBaseStats)
async def list_files(user: dict = Depends(get_current_user)):
    """列出知识库文件"""
    KB_OWNER_ID = 1
    files = get_files_by_owner(KB_OWNER_ID)
    from rag.vector_store import get_collection_stats
    stats = get_collection_stats(KB_OWNER_ID)

    return KnowledgeBaseStats(
        total_files=len(files),
        total_chunks=stats["total_chunks"],
        files=[
            FileMetaOut(
                id=f["id"],
                owner_id=f["owner_id"],
                filename=f["filename"],
                filepath=f["filepath"],
                file_size=f.get("file_size", 0),
                chunk_count=f.get("chunk_count", 0),
                created_at=f["created_at"],
            )
            for f in files
        ],
    )


@router.post("/upload", response_model=APIResponse)
async def upload_files(
    files: list[UploadFile] = File(...),
    user: dict = Depends(require_admin),
):
    """
    批量上传文件（异步处理 + 文件去重 + 进度追踪）。
    返回 task_ids 列表，前端轮询 GET /api/kb/upload-status/{task_id}
    """
    if not files:
        raise HTTPException(status_code=400, detail="请选择要上传的文件")

    owner_id = user["owner_id"]
    conn = _get_conn()
    task_ids = []
    skipped_already = []

    for file in files:
        filename = file.filename
        file_size = 0
        try:
            content = await file.read()
            file_size = len(content)

            # 写入临时文件
            with tempfile.NamedTemporaryFile(delete=False, suffix=Path(filename).suffix) as tmp:
                tmp.write(content)
                tmp_path = tmp.name

            # 快速 hash 去重（同步检查，快）
            file_hash = _compute_file_hash(tmp_path)
            existing = _check_file_exists(owner_id, file_hash, filename)
            if existing:
                skipped_already.append(f"{filename}（已存在 # {existing['id']}）")
                os.unlink(tmp_path)
                continue

            # 创建任务记录
            task_id = str(uuid.uuid4())[:12]
            conn.execute(
                "INSERT INTO upload_tasks (task_id, owner_id, filename, file_size, status) VALUES (?, ?, ?, ?, 'pending')",
                (task_id, owner_id, filename, file_size),
            )
            conn.commit()
            task_ids.append(task_id)

            # 提交异步处理
            async_queue.enqueue(
                _process_file_async,
                task_id, tmp_path, filename, owner_id,
            )

        except Exception as e:
            logger.error(f"[KB] 文件提交失败: {filename} - {e}")

    conn.close()

    msg = f"已提交 {len(task_ids)} 个文件异步处理"
    if skipped_already:
        msg += f"（{len(skipped_already)} 个已存在跳过）"

    return APIResponse(
        success=True,
        message=msg,
        data={
            "task_ids": task_ids,
            "skipped": skipped_already,
            "total_submitted": len(task_ids),
        },
    )


@router.get("/upload-status/{task_id}")
async def get_upload_status(task_id: str, user: dict = Depends(get_current_user)):
    """查询上传任务进度"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM upload_tasks WHERE task_id = ? AND owner_id = ?",
        (task_id, user["owner_id"]),
    ).fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")

    return {
        "success": True,
        "task_id": row["task_id"],
        "filename": row["filename"],
        "status": row["status"],       # pending / processing / done / failed / skipped
        "progress": row["progress"],    # 0-100
        "chunk_count": row["chunk_count"],
        "error": row["error"],
        "created_at": row["created_at"],
    }


@router.delete("/files/{file_id}", response_model=APIResponse)
async def delete_file(file_id: int, user: dict = Depends(require_admin)):
    """删除指定文件（仅管理员）"""
    owner_id = user["owner_id"]
    from rag.vector_store import delete_by_file
    file_record = get_file_by_id(file_id)
    if not file_record:
        raise HTTPException(status_code=404, detail="文件不存在")
    if file_record["owner_id"] != owner_id:
        raise HTTPException(status_code=403, detail="无权操作此文件")

    filename = file_record["filename"]
    filepath = file_record["filepath"]

    deleted_chunks = delete_by_file(filename, owner_id)
    if os.path.exists(filepath):
        os.remove(filepath)
    delete_file_record(file_id, owner_id)
    logger.info(f"[KB] 删除文件: {filename}, {deleted_chunks} 块")

    return APIResponse(success=True, message=f"已删除 {filename}（{deleted_chunks} 个向量块）")


@router.delete("/clear", response_model=APIResponse)
async def clear_knowledge_base(user: dict = Depends(require_admin)):
    """清空知识库（仅管理员）"""
    owner_id = user["owner_id"]
    from rag.vector_store import delete_all_by_owner, reset_vector_store
    deleted_chunks = delete_all_by_owner(owner_id)
    deleted_files = delete_all_file_records(owner_id)
    upload_dir = str(settings.resolve_path(settings.upload_dir))
    for f in os.listdir(upload_dir):
        fpath = os.path.join(upload_dir, f)
        if os.path.isfile(fpath):
            os.remove(fpath)
    reset_vector_store()
    logger.info(f"[KB] 清空: {deleted_chunks} 块, {deleted_files} 文件")
    return APIResponse(success=True, message=f"已清空（{deleted_chunks} 块, {deleted_files} 文件）")


@router.post("/rebuild", response_model=APIResponse)
async def rebuild_knowledge_base(user: dict = Depends(require_admin)):
    """重建知识库（仅管理员）"""
    owner_id = user["owner_id"]
    from rag.vector_store import delete_all_by_owner, reset_vector_store, add_documents
    from rag.document_loader import load_documents_from_paths
    from rag.text_splitter import process_documents_batch
    delete_all_by_owner(owner_id)
    reset_vector_store()

    files = get_files_by_owner(owner_id)
    upload_dir = str(settings.resolve_path(settings.upload_dir))
    total_chunks = 0

    for file_record in files:
        filepath = file_record["filepath"]
        if not os.path.exists(filepath):
            continue
        try:
            docs = load_documents_from_paths([filepath], upload_dir)
            if not docs:
                continue
            processed = process_documents_batch(docs)
            if not processed or not processed[0].get("chunks"):
                continue
            chunks = processed[0]["chunks"]
            metadatas = [
                {"owner_id": owner_id, "source": file_record["filename"], "chunk_idx": i, "filepath": filepath}
                for i in range(len(chunks))
            ]
            add_documents(chunks, metadatas)
            update_file_chunk_count(file_record["id"], len(chunks))
            total_chunks += len(chunks)
        except Exception as e:
            logger.error(f"[KB] 重建失败: {file_record['filename']} - {e}")

    logger.info(f"[KB] 重建完成: {len(files)} 文件, {total_chunks} 块")
    return APIResponse(success=True, message=f"重建完成（{len(files)} 文件, {total_chunks} 块）")
