"""
知识库管理路由
"""
import os
import shutil
import tempfile
from pathlib import Path
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Form
from loguru import logger

from core.schemas import FileUploadResponse, KnowledgeBaseStats, FileMetaOut, APIResponse
from core.auth import get_current_user
from core.database import (
    get_files_by_owner,
    get_file_by_id,
    delete_file_record,
    delete_all_file_records,
    insert_file_record,
    update_file_chunk_count,
)
from rag.document_loader import load_documents_from_paths
from rag.text_splitter import process_documents_batch
from rag.vector_store import (
    add_documents,
    delete_by_file,
    delete_all_by_owner,
    get_collection_stats,
    reset_vector_store,
)
from config.settings import settings

router = APIRouter(prefix="/api/kb", tags=["知识库"])


@router.get("/files", response_model=KnowledgeBaseStats)
async def list_files(user: dict = Depends(get_current_user)):
    """列出当前用户的所有已上传文件"""
    files = get_files_by_owner(user["owner_id"])
    stats = get_collection_stats(user["owner_id"])

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
    user: dict = Depends(get_current_user),
):
    """
    批量上传文件并入库。
    支持 PDF/Word/Excel/TXT/MD/图片/ZIP。
    """
    if not files:
        raise HTTPException(status_code=400, detail="请选择要上传的文件")

    upload_dir = str(settings.resolve_path(settings.upload_dir))
    owner_id = user["owner_id"]

    total_chunks = 0
    success_files = []
    failed_files = []

    for file in files:
        filename = file.filename
        logger.info(f"[KB API] 上传文件: {filename}, user={user['username']}")

        try:
            # 保存到临时文件
            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=Path(filename).suffix,
            ) as tmp:
                content = await file.read()
                tmp.write(content)
                tmp_path = tmp.name

            file_size = len(content)

            # 解析文档
            docs = load_documents_from_paths([tmp_path], upload_dir)

            if not docs or not docs[0].get("content", "").strip():
                failed_files.append({"filename": filename, "reason": "无法解析内容"})
                os.unlink(tmp_path)
                continue

            # 文本分块
            processed = process_documents_batch(docs)

            if not processed or not processed[0].get("chunks"):
                failed_files.append({"filename": filename, "reason": "内容为空"})
                os.unlink(tmp_path)
                continue

            chunks = processed[0]["chunks"]

            # 持久化文件
            dest_path = os.path.join(upload_dir, filename)
            shutil.copy2(tmp_path, dest_path)
            os.unlink(tmp_path)

            # 向量化入库
            metadatas = [
                {
                    "owner_id": owner_id,
                    "source": filename,
                    "chunk_idx": i,
                    "filepath": dest_path,
                }
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

            total_chunks += len(chunks)
            success_files.append(filename)

        except Exception as e:
            logger.error(f"[KB API] 文件上传失败: {filename} - {e}")
            failed_files.append({"filename": filename, "reason": str(e)[:200]})

    message = (
        f"上传完成。成功: {len(success_files)} 个文件 ({total_chunks} 块)"
        + (f"，失败: {len(failed_files)}" if failed_files else "")
    )

    return APIResponse(
        success=len(success_files) > 0,
        message=message,
        data={
            "success_files": success_files,
            "failed_files": failed_files,
            "total_chunks": total_chunks,
        },
    )


@router.delete("/files/{file_id}", response_model=APIResponse)
async def delete_file(
    file_id: int,
    user: dict = Depends(get_current_user),
):
    """删除指定文件（知识库 + 磁盘 + 数据库）"""
    owner_id = user["owner_id"]

    file_record = get_file_by_id(file_id)
    if not file_record:
        raise HTTPException(status_code=404, detail="文件不存在")
    if file_record["owner_id"] != owner_id:
        raise HTTPException(status_code=403, detail="无权操作此文件")

    filename = file_record["filename"]
    filepath = file_record["filepath"]

    # 从向量库删除
    deleted_chunks = delete_by_file(filename, owner_id)

    # 从磁盘删除
    if os.path.exists(filepath):
        os.remove(filepath)

    # 从数据库删除
    delete_file_record(file_id, owner_id)

    logger.info(f"[KB API] 删除文件: {filename}, {deleted_chunks} 向量块")

    return APIResponse(
        success=True,
        message=f"已删除 {filename}（{deleted_chunks} 个向量块）",
    )


@router.delete("/clear", response_model=APIResponse)
async def clear_knowledge_base(user: dict = Depends(get_current_user)):
    """清空当前用户的所有知识库数据"""
    owner_id = user["owner_id"]

    deleted_chunks = delete_all_by_owner(owner_id)
    deleted_files = delete_all_file_records(owner_id)

    # 清理上传文件目录
    upload_dir = str(settings.resolve_path(settings.upload_dir))
    for f in os.listdir(upload_dir):
        fpath = os.path.join(upload_dir, f)
        if os.path.isfile(fpath):
            os.remove(fpath)

    reset_vector_store()

    logger.info(f"[KB API] 清空知识库: {deleted_chunks} 块, {deleted_files} 文件")

    return APIResponse(
        success=True,
        message=f"知识库已清空（{deleted_chunks} 个向量块, {deleted_files} 个文件）",
    )


@router.post("/rebuild", response_model=APIResponse)
async def rebuild_knowledge_base(user: dict = Depends(get_current_user)):
    """重建知识库：重新处理所有已上传文件"""
    owner_id = user["owner_id"]

    # 清空向量库
    delete_all_by_owner(owner_id)
    reset_vector_store()

    # 重新处理所有文件
    files = get_files_by_owner(owner_id)
    upload_dir = str(settings.resolve_path(settings.upload_dir))

    total_chunks = 0
    for file_record in files:
        filepath = file_record["filepath"]
        filename = file_record["filename"]

        if not os.path.exists(filepath):
            logger.warning(f"[KB API] 重建跳过（文件不存在）: {filepath}")
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
                {
                    "owner_id": owner_id,
                    "source": filename,
                    "chunk_idx": i,
                    "filepath": filepath,
                }
                for i in range(len(chunks))
            ]
            add_documents(chunks, metadatas)

            update_file_chunk_count(file_record["id"], len(chunks))
            total_chunks += len(chunks)

        except Exception as e:
            logger.error(f"[KB API] 重建失败: {filename} - {e}")

    logger.info(f"[KB API] 知识库重建完成: {len(files)} 文件, {total_chunks} 块")

    return APIResponse(
        success=True,
        message=f"知识库重建完成（{len(files)} 个文件, {total_chunks} 个向量块）",
    )
