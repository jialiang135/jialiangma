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
from core.paths import safe_filename, safe_join, remove_within
from core.async_queue import async_queue
from config.settings import settings, PROJECT_ROOT
from rag.document_loader import SUPPORTED_EXTENSIONS

DB_PATH = PROJECT_ROOT / "assets" / "personal_agent.db"

router = APIRouter(prefix="/api/kb", tags=["知识库"])

# 允许上传的扩展名 —— 直接取自文档加载器白名单，保持单一真相源。
# loader 内部对 .zip 走特殊分支（load_zip），所以要单独并进来。
ALLOWED_UPLOAD_EXTENSIONS = frozenset(SUPPORTED_EXTENSIONS) | {".zip"}

# 用于「Content-Type 与扩展名是否明显不符」的告警（仅图片/PDF 这类明确的家族）
_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".png": "image/",
    ".jpg": "image/",
    ".jpeg": "image/",
    ".bmp": "image/",
    ".tiff": "image/",
}


# ─── 辅助函数 ───

def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _validate_upload(file: UploadFile) -> tuple[str | None, str | None]:
    """
    校验单个上传文件。

    Returns:
        ``(净化后的文件名, 错误原因)`` —— 两者恰有一个为 None。
    """
    raw_name = file.filename or ""

    # 1. 文件名净化 —— 路径穿越的唯一防线，必须最先做
    clean = safe_filename(raw_name, fallback="")
    if not clean:
        return None, f"文件名不合法: {raw_name!r}"

    # 2. 扩展名白名单：不合规在入口就拒绝，而不是落盘后等解析任务失败
    ext = Path(clean).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return None, f"不支持的文件格式: {ext or '(无扩展名)'}"

    # 3. 大小上限（file.size 由 multipart 解析器填入；落盘时还会再兜一次）
    if file.size is not None and file.size > settings.max_upload_size_mb * 1024 * 1024:
        return None, f"文件超过 {settings.max_upload_size_mb}MB 上限"

    # 4. Content-Type 与扩展名明显不符时告警（不拦截，理由见函数说明）
    _warn_on_mime_mismatch(clean, file.content_type)

    return clean, None


def _warn_on_mime_mismatch(filename: str, content_type: str | None) -> None:
    """
    声明的 Content-Type 与扩展名明显不符时记告警，但**不拦截**。

    不硬拦截的原因：Content-Type 完全由客户端决定且各家差异很大
    （``.md`` 可能是 ``text/markdown`` / ``text/plain`` /
    ``application/octet-stream``），硬拦截会误伤正常上传。真正的类型安全
    由 document_loader 按扩展名分派解析器来保证。
    """
    if not content_type:
        return
    expected = _MIME_BY_EXT.get(Path(filename).suffix.lower())
    if expected and not content_type.lower().startswith(expected):
        logger.warning(
            "上传 Content-Type 与扩展名不符: {} 声明={} 预期前缀={}",
            filename,
            content_type,
            expected,
        )


async def _spool_to_temp(file: UploadFile, suffix: str, max_bytes: int) -> tuple[str, int]:
    """
    分块把上传内容写入临时文件，返回 ``(临时文件路径, 字节数)``。

    不用 ``await file.read()`` 一次性读入内存：那样单个大文件就会整份驻留内存，
    批量上传时内存放大 N 倍。这里改为 1MB 分块，并在累积过程中硬性拦截超限
    （不依赖客户端的 Content-Length，避免被伪造绕过）。
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    size = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件超过 {settings.max_upload_size_mb}MB 上限",
                )
            tmp.write(chunk)
        tmp.close()
        return tmp_path, size
    except BaseException:
        # 超限 / 客户端断开 / 磁盘错误：都不要留下半成品临时文件
        tmp.close()
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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

    # 持久化：文件名已在入口净化过，这里再用 safe_join 做一次纵深防御
    dest_path = str(safe_join(upload_dir, filename))
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

    校验分两层：文件名/扩展名/大小在**入口**就拒绝（返回 400/413），
    内容能否解析交给异步任务的 status 字段。
    """
    if not files:
        raise HTTPException(status_code=400, detail="请选择要勾选的文件")

    owner_id = user["owner_id"]
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    conn = _get_conn()
    task_ids = []
    skipped_already = []
    rejected = []

    try:
        for file in files:
            # ── 1. 入口校验：不合规的文件直接记录原因，不落盘、不建任务 ──
            filename, err = _validate_upload(file)
            if err:
                rejected.append({"filename": file.filename, "reason": err})
                logger.warning("[KB] 拒绝上传: {} - {}", file.filename, err)
                continue

            tmp_path = ""
            task_id = None
            try:
                # ── 2. 分块落盘（带超限硬拦截） ──
                tmp_path, file_size = await _spool_to_temp(
                    file, Path(filename).suffix, max_bytes
                )

                # ── 3. 快速 hash 去重 ──
                file_hash = _compute_file_hash(tmp_path)
                existing = _check_file_exists(owner_id, file_hash, filename)
                if existing:
                    skipped_already.append(f"{filename}（已存在 # {existing['id']}）")
                    os.unlink(tmp_path)
                    continue

                # ── 4. 建任务记录 ──
                task_id = str(uuid.uuid4())[:12]
                conn.execute(
                    "INSERT INTO upload_tasks (task_id, owner_id, filename, file_size, status) VALUES (?, ?, ?, ?, 'pending')",
                    (task_id, owner_id, filename, file_size),
                )
                conn.commit()

                # ── 5. 提交异步处理 ──
                # 必须先确认提交成功再计入 task_ids。反过来的话，入队失败时
                # 同一个文件会同时出现在 task_ids 和 rejected 里（自相矛盾），
                # 前端还会去轮询一个永远不会推进的任务。
                async_queue.enqueue(
                    _process_file_async,
                    task_id, tmp_path, filename, owner_id,
                )
                task_ids.append(task_id)

            except HTTPException as e:
                # 超限等客户端错误：清理临时文件后记为 rejected，而不是 500
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                rejected.append({"filename": filename, "reason": str(e.detail)})
                logger.warning("[KB] 拒绝上传: {} - {}", filename, e.detail)
            except Exception as e:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                # 已经把任务行写进库了，但没能提交给队列 —— 标记为失败，
                # 否则前端会一直轮询到一个永久 pending 的任务
                if task_id:
                    try:
                        conn.execute(
                            "UPDATE upload_tasks SET status='failed', progress=100, error=?, "
                            "updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                            (f"任务提交失败: {str(e)[:200]}", task_id),
                        )
                        conn.commit()
                    except Exception as db_err:
                        logger.error("[KB] 标记任务失败也失败了: {}", db_err)
                rejected.append({"filename": filename, "reason": f"提交失败: {str(e)[:100]}"})
                logger.error(f"[KB] 文件提交失败: {filename} - {e}")
    finally:
        conn.close()

    msg = f"已提交 {len(task_ids)} 个文件异步处理"
    if skipped_already:
        msg += f"（{len(skipped_already)} 个已存在跳过）"
    if rejected:
        msg += f"（{len(rejected)} 个被拒绝）"

    # 全部被拒 → 明确返回 400，让前端能直接提示原因
    if not task_ids and not skipped_already and rejected:
        raise HTTPException(status_code=400, detail={"message": msg, "rejected": rejected})

    return APIResponse(
        success=True,
        message=msg,
        data={
            "task_ids": task_ids,
            "skipped": skipped_already,
            "rejected": rejected,
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

    # 走 remove_within 而非直接 os.remove：库里可能存在早期版本写入的
    # 穿越路径（那时文件名没净化），直接删就是任意文件删除。
    upload_dir = str(settings.resolve_path(settings.upload_dir))
    if not remove_within(upload_dir, filepath):
        logger.warning("[KB] 磁盘文件未删除（越界或不存在）: {}", filepath)

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
