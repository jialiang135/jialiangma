"""
知识库管理路由（文件去重 + 异步处理 + 进度查询）

线程模型说明
------------
两条路径的性能特征不同，处理方式也不同：

1. **请求路径**（本文件的 ``@router`` 函数）跑在事件循环里，所以：
   - 数据库调用直接 ``await``（数据层已是 async）
   - Chroma 操作、文件 IO 这类同步调用用 ``asyncio.to_thread`` 挪出去
2. **后台任务**（``core/kb_tasks`` 里的函数）跑在线程池的工作线程里，那里
   **没有 event loop**，而这正是我们要的"重活离开事件循环"。它们保持同步，
   内部的数据库调用通过 ``run_async_from_thread`` 提交回主循环执行。
"""

import asyncio
import contextlib
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from loguru import logger

from config.settings import settings
from core.async_queue import async_queue
from core.auth import get_current_user, require_admin
from core.database import (
    create_upload_task,
    delete_all_file_records,
    delete_file_record,
    find_file_by_hash_or_name,
    get_file_by_id,
    get_files_by_owner,
    get_upload_task,
    update_upload_task,
)
from core.kb_tasks import (
    compute_file_hash,
    process_file_task,
    rebuild_task,
)
from core.paths import remove_within, safe_filename
from core.schemas import APIResponse, FileMetaOut, KnowledgeBaseStats
from rag.document_loader import SUPPORTED_EXTENSIONS

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

# 重建任务在 upload_tasks 里的占位文件名（前端据此区分上传任务与重建任务）
REBUILD_TASK_FILENAME = "__rebuild__"


# ─── 校验 ───


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
    # 刻意不用 with：成功时文件要保留（交给后台任务处理），失败时才清理，
    # 生命周期由下面的 try/except 显式管理
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)  # noqa: SIM115
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
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _clear_upload_dir(upload_dir: Path) -> None:
    """清空上传目录（同步，跑在线程池）。"""
    if not upload_dir.exists():
        return
    for entry in upload_dir.iterdir():
        if entry.is_file():
            try:
                entry.unlink()
            except OSError as e:
                logger.warning("清理文件失败: {} - {}", entry, e)


# ─── 路由 ───


@router.get("/files", response_model=KnowledgeBaseStats)
async def list_files(user: dict = Depends(get_current_user)):
    """
    列出知识库文件。

    知识库属于管理员（`settings.shared_kb_owner_id`），这里的归属不再写死 1。
    注意本端点只要求登录、不要求管理员 —— 也就是说**任何已登录用户都能看到
    文件名列表**。前端「知识库」页对所有用户开放，所以这是当前的既定行为；
    若希望只让管理员看到，需要改 `require_admin` 并同时调整前端导航。
    """
    kb_owner = settings.shared_kb_owner_id
    files = await get_files_by_owner(kb_owner)
    from rag.vector_store import get_collection_stats

    stats = await asyncio.to_thread(get_collection_stats, kb_owner)

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
        raise HTTPException(status_code=400, detail="请选择要上传的文件")

    owner_id = user["owner_id"]
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    task_ids = []
    skipped_already = []
    rejected = []

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
            tmp_path, file_size = await _spool_to_temp(file, Path(filename).suffix, max_bytes)

            # ── 3. 快速 hash 去重 ──
            file_hash = await asyncio.to_thread(compute_file_hash, tmp_path)
            existing = await find_file_by_hash_or_name(owner_id, file_hash, filename)
            if existing:
                skipped_already.append(f"{filename}（已存在 # {existing['id']}）")
                os.unlink(tmp_path)
                continue

            # ── 4. 建任务记录 ──
            task_id = str(uuid.uuid4())[:12]
            await create_upload_task(task_id, owner_id, filename, file_size)

            # ── 5. 提交异步处理 ──
            # 必须先确认提交成功再计入 task_ids。反过来的话，入队失败时
            # 同一个文件会同时出现在 task_ids 和 rejected 里（自相矛盾），
            # 前端还会去轮询一个永远不会推进的任务。
            async_queue.enqueue(
                process_file_task,
                task_id,
                tmp_path,
                filename,
                owner_id,
            )
            task_ids.append(task_id)

        except HTTPException as e:
            # 超限等客户端错误：清理临时文件后记为 rejected，而不是 500
            if tmp_path:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
            rejected.append({"filename": filename, "reason": str(e.detail)})
            logger.warning("[KB] 拒绝上传: {} - {}", filename, e.detail)
        except Exception as e:
            if tmp_path:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
            # 已经把任务行写进库了，但没能提交给队列 —— 标记为失败，
            # 否则前端会一直轮询到一个永久 pending 的任务
            if task_id:
                try:
                    await update_upload_task(
                        task_id,
                        status="failed",
                        progress=100,
                        error=f"任务提交失败: {str(e)[:200]}",
                    )
                except Exception as db_err:
                    logger.error("[KB] 标记任务失败也失败了: {}", db_err)
            rejected.append({"filename": filename, "reason": f"提交失败: {str(e)[:100]}"})
            logger.error(f"[KB] 文件提交失败: {filename} - {e}")

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
    """查询上传/重建任务进度"""
    task = await get_upload_task(task_id, user["owner_id"])
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    return {
        "success": True,
        "task_id": task["task_id"],
        "filename": task["filename"],
        "status": task["status"],  # pending / processing / done / failed / skipped
        "progress": task["progress"],  # 0-100
        "chunk_count": task["chunk_count"],
        "error": task["error"],
        "is_rebuild": task["filename"] == REBUILD_TASK_FILENAME,
        "created_at": task["created_at"],
    }


@router.delete("/files/{file_id}", response_model=APIResponse)
async def delete_file(file_id: int, user: dict = Depends(require_admin)):
    """删除指定文件（仅管理员）"""
    owner_id = user["owner_id"]
    from rag.vector_store import delete_by_file

    file_record = await get_file_by_id(file_id)
    if not file_record:
        raise HTTPException(status_code=404, detail="文件不存在")
    if file_record["owner_id"] != owner_id:
        raise HTTPException(status_code=403, detail="无权操作此文件")

    filename = file_record["filename"]
    filepath = file_record["filepath"]

    deleted_chunks = await asyncio.to_thread(delete_by_file, filename, owner_id)

    # 走 remove_within 而非直接 os.remove：库里可能存在早期版本写入的
    # 穿越路径（那时文件名没净化），直接删就是任意文件删除。
    upload_dir = str(settings.resolve_path(settings.upload_dir))
    if not remove_within(upload_dir, filepath):
        logger.warning("[KB] 磁盘文件未删除（越界或不存在）: {}", filepath)

    await delete_file_record(file_id, owner_id)
    logger.info(f"[KB] 删除文件: {filename}, {deleted_chunks} 块")

    return APIResponse(success=True, message=f"已删除 {filename}（{deleted_chunks} 个向量块）")


@router.delete("/clear", response_model=APIResponse)
async def clear_knowledge_base(user: dict = Depends(require_admin)):
    """清空知识库（仅管理员）"""
    owner_id = user["owner_id"]
    from rag.vector_store import delete_all_by_owner, reset_vector_store

    deleted_chunks = await asyncio.to_thread(delete_all_by_owner, owner_id)
    deleted_files = await delete_all_file_records(owner_id)

    upload_dir = settings.resolve_path(settings.upload_dir)
    await asyncio.to_thread(_clear_upload_dir, upload_dir)
    await asyncio.to_thread(reset_vector_store)

    logger.info(f"[KB] 清空: {deleted_chunks} 块, {deleted_files} 文件")
    return APIResponse(success=True, message=f"已清空（{deleted_chunks} 块, {deleted_files} 文件）")


@router.post("/rebuild", response_model=APIResponse)
async def rebuild_knowledge_base(user: dict = Depends(require_admin)):
    """
    重建知识库（仅管理员）。

    这是个可能耗时数分钟到数小时的操作（PDF 解析 + OCR + 逐块向量化），
    因此**提交为后台任务并立刻返回 task_id**，由前端轮询
    ``GET /api/kb/upload-status/{task_id}`` 获取进度。
    原实现是在请求里同步跑完整个循环 —— 期间整个进程的所有请求都会被卡住。
    """
    owner_id = user["owner_id"]
    task_id = str(uuid.uuid4())[:12]
    await create_upload_task(task_id, owner_id, REBUILD_TASK_FILENAME, 0)

    try:
        async_queue.enqueue(rebuild_task, task_id, owner_id)
    except Exception as e:
        await update_upload_task(
            task_id, status="failed", progress=100, error=f"任务提交失败: {str(e)[:200]}"
        )
        raise HTTPException(status_code=503, detail=f"重建任务提交失败: {str(e)[:200]}") from e

    logger.info(f"[KB] 重建任务已提交: task_id={task_id}")
    return APIResponse(
        success=True,
        message="重建任务已提交，可通过任务状态查询进度",
        data={"task_id": task_id},
    )
