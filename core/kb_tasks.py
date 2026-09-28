"""
知识库后台任务
==============

文档解析、分块、向量化、重建 —— 这些是 CPU/IO 密集的**同步**工作，
统一放在这里的原因有三：

1. 它们必须跑在**工作线程**里（不能占用事件循环），所以保持同步函数；
2. 内部的数据库调用通过 ``run_async_from_thread`` 提交回主循环执行
   （数据层是 async 的）；
3. 原本这套逻辑同时存在于 ``api/routes/kb_routes.py`` 与
   ``agent/manage_agent.py`` 两处，重复且容易漂移，这里收口成一份。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil

from loguru import logger

from config.settings import settings
from core.database import (
    find_file_by_hash_or_name,
    get_files_by_owner,
    insert_file_record,
    run_async_from_thread,
    update_file_chunk_count,
    update_file_hash,
    update_upload_task,
)
from core.paths import safe_join
from core.telemetry import span


def compute_file_hash(filepath: str) -> str:
    """计算文件 MD5（同步读文件，应在线程池里调用）。"""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def process_file_sync(file_path: str, filename: str, owner_id: int) -> dict:
    """
    处理单个文件：解析 → 分块 → 向量化 → 落库。

    **同步函数**，应在工作线程中执行。
    """
    from rag.document_loader import load_documents_from_paths
    from rag.text_splitter import process_documents_batch
    from rag.vector_store import add_documents

    file_size = os.path.getsize(file_path)
    upload_dir = str(settings.resolve_path(settings.upload_dir))

    # 解析（PDF/OCR 可能是分钟级）与分块各记一个 span，便于定位慢在哪一步
    with span("document.parse", filename=filename, file_size=file_size):
        docs = load_documents_from_paths([file_path], upload_dir)
    if not docs or not docs[0].get("content", "").strip():
        return {"success": False, "error": "无法解析文件内容"}

    with span(
        "document.split",
        filename=filename,
        semantic_splitter=settings.use_semantic_splitter,
    ):
        processed = process_documents_batch(
            docs, use_semantic_splitter=settings.use_semantic_splitter
        )
    if not processed or not processed[0].get("chunks"):
        return {"success": False, "error": "内容为空"}

    chunks = processed[0]["chunks"]

    # 文件名在入口已净化，这里再用 safe_join 做一次纵深防御
    dest_path = str(safe_join(upload_dir, filename))
    shutil.copy2(file_path, dest_path)

    file_hash = compute_file_hash(dest_path)
    existing = run_async_from_thread(find_file_by_hash_or_name(owner_id, file_hash, filename))
    if existing:
        os.unlink(dest_path)
        return {
            "success": True,
            "skipped": True,
            "message": f"文件已存在（# {existing['id']}），跳过重复上传",
            "existing_id": existing["id"],
        }

    metadatas = [
        {"owner_id": owner_id, "source": filename, "chunk_idx": i, "filepath": dest_path}
        for i in range(len(chunks))
    ]
    # 向量化是最慢的一步（每批一次 DashScope HTTP），单独计时
    with span("document.embed", filename=filename, chunks=len(chunks)):
        add_documents(chunks, metadatas)

    file_id = run_async_from_thread(
        insert_file_record(
            owner_id=owner_id,
            filename=filename,
            filepath=dest_path,
            file_size=file_size,
            chunk_count=len(chunks),
        )
    )
    run_async_from_thread(update_file_chunk_count(file_id, len(chunks)))
    run_async_from_thread(update_file_hash(file_id, file_hash))

    return {"success": True, "filename": filename, "chunks": len(chunks), "file_id": file_id}


def process_file_task(task_id: str, tmp_path: str, filename: str, owner_id: int) -> None:
    """上传任务包装：处理文件并更新 upload_tasks 状态。同步，线程池执行。"""
    try:
        run_async_from_thread(update_upload_task(task_id, status="processing", progress=30))

        result = process_file_sync(tmp_path, filename, owner_id)
        if result.get("success"):
            if result.get("skipped"):
                run_async_from_thread(
                    update_upload_task(
                        task_id,
                        status="skipped",
                        progress=100,
                        error=result.get("message", "跳过"),
                    )
                )
            else:
                run_async_from_thread(
                    update_upload_task(
                        task_id,
                        status="done",
                        progress=100,
                        chunk_count=result.get("chunks", 0),
                    )
                )
        else:
            run_async_from_thread(
                update_upload_task(
                    task_id,
                    status="failed",
                    progress=100,
                    error=result.get("error", "未知错误"),
                )
            )
    except Exception as e:
        logger.error("[KB] 文件处理失败: {} - {}", filename, e)
        try:
            run_async_from_thread(
                update_upload_task(
                    task_id,
                    status="failed",
                    progress=100,
                    error=str(e)[:500],
                )
            )
        except Exception as inner:
            logger.error("[KB] 标记任务失败也失败了: {}", inner)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def rebuild_knowledge_base(owner_id: int) -> int:
    """
    重建整个知识库，返回总向量块数。

    **同步函数**，应在工作线程中执行 —— 这个操作可能耗时数分钟到数小时
    （PDF 解析 + OCR + 逐块向量化），绝不能进事件循环。
    """
    from rag.document_loader import load_documents_from_paths
    from rag.text_splitter import process_documents_batch
    from rag.vector_store import add_documents, delete_all_by_owner, reset_vector_store

    delete_all_by_owner(owner_id)
    reset_vector_store()

    files = run_async_from_thread(get_files_by_owner(owner_id))
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
            processed = process_documents_batch(
                docs, use_semantic_splitter=settings.use_semantic_splitter
            )
            if not processed or not processed[0].get("chunks"):
                continue
            chunks = processed[0]["chunks"]
            metadatas = [
                {
                    "owner_id": owner_id,
                    "source": file_record["filename"],
                    "chunk_idx": i,
                    "filepath": filepath,
                }
                for i in range(len(chunks))
            ]
            add_documents(chunks, metadatas)
            run_async_from_thread(update_file_chunk_count(file_record["id"], len(chunks)))
            total_chunks += len(chunks)
        except Exception as e:
            logger.error("[KB] 重建失败: {} - {}", file_record["filename"], e)

    return total_chunks


def rebuild_task(task_id: str, owner_id: int) -> None:
    """重建任务包装：更新 upload_tasks 状态。同步，线程池执行。"""
    try:
        run_async_from_thread(update_upload_task(task_id, status="processing", progress=10))
        total_chunks = rebuild_knowledge_base(owner_id)
        run_async_from_thread(
            update_upload_task(
                task_id,
                status="done",
                progress=100,
                chunk_count=total_chunks,
            )
        )
        logger.info("[KB] 重建完成: {} 块", total_chunks)
    except Exception as e:
        logger.error("[KB] 重建失败: {}", e)
        try:
            run_async_from_thread(
                update_upload_task(
                    task_id,
                    status="failed",
                    progress=100,
                    error=str(e)[:500],
                )
            )
        except Exception as inner:
            logger.error("[KB] 标记重建失败也失败了: {}", inner)
