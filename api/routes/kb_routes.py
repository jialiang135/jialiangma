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
import mimetypes
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from loguru import logger

from config.settings import settings
from core.async_queue import async_queue
from core.auth import get_current_user, require_admin
from core.chunking import (
    chunking_description,
    load_chunking_config,
    save_chunking_config,
    validate_chunking_payload,
)
from core.db.files import (
    create_upload_task,
    delete_all_file_records,
    delete_file_record,
    find_file_by_hash_or_name,
    get_file_by_id,
    get_files_by_owner,
    get_upload_task,
    update_upload_task,
)
from core.kb_access import resolve_kb_owner
from core.kb_tasks import (
    compute_file_hash,
    process_file_task,
    rebuild_task,
)
from core.paths import ensure_within, remove_within, safe_filename
from core.schemas import (
    APIResponse,
    ChunkingConfigUpdate,
    ChunkingPreviewRequest,
    FileMetaOut,
    KnowledgeBaseStats,
)
from rag.document_loader import SUPPORTED_EXTENSIONS

router = APIRouter(prefix="/api/kb", tags=["知识库"])


def _kb_owner(user: dict) -> int:
    """
    知识库管理操作（上传 / 删除 / 清空 / 重建）的目标 owner。

    **必须与 `/kb/files`（读）走同一个 owner**，规则本身在
    ``core/kb_access.py``，这里只是调用点。

    原来这里写的是 ``user["owner_id"]``，而读侧用的是
    ``settings.shared_kb_owner_id`` —— 两者对"admin 角色但 owner_id 不等于
    共享 owner"的账号（本机上实际就有）不一致，表现为**看到的和改的不是同一个库**：

    - 上传：文件落进自己的库，聊天检索不到（聊天查的是共享库）
    - 重建：重建自己那个空库 → 日志 "重建结束: 成功 0/0 文件, 0 块"，共享库一点没动
    - 删除：拿共享库的文件 ID 去比对，``owner_id`` 不匹配 → 报"文件不存在"
    - 清空：清空自己的库，界面刷新后文件**还在**（因为显示的是共享库）

    实测踩到的是"重建 0 个文件"。这条与 ``core/kb_access.py`` 记录的那次越权
    （读侧写死 1）是同一类问题的两个面：**归属规则散落在各接口，没有单一裁决点。**
    """
    return resolve_kb_owner(user["owner_id"])


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


@router.get("/formats")
async def get_supported_formats():
    """
    上传支持的格式清单（登录可读）。

    为什么要有这个接口：前端文件选择器的 ``accept`` 与页面上的"支持 XX 格式"文案
    需要和后端**同一份**清单。原先前端是手抄的，抄漏了 .doc/.xls/.csv/.jpeg/.bmp/
    .tiff —— 用户以为不支持，切到"所有文件"却又能传上去，两边说法不一致。
    """
    return {
        "success": True,
        "data": {
            "extensions": sorted(ALLOWED_UPLOAD_EXTENSIONS),
            # 给界面文案用的分组（不是白名单本身，白名单以上面为准）
            "groups": [
                {"label": "文档", "exts": [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv"]},
                {"label": "文本", "exts": [".txt", ".md", ".json", ".py"]},
                {"label": "图片（OCR）", "exts": [".png", ".jpg", ".jpeg", ".bmp", ".tiff"]},
                {"label": "压缩包", "exts": [".zip"]},
            ],
        },
    }


@router.get("/files", response_model=KnowledgeBaseStats)
async def list_files(user: dict = Depends(get_current_user)):
    """
    列出知识库文件。

    知识库属于管理员（`settings.shared_kb_owner_id`），这里的归属不再写死 1。
    注意本端点只要求登录、不要求管理员 —— 也就是说**任何已登录用户都能看到
    文件名列表**。前端「知识库」页对所有用户开放，所以这是当前的既定行为；
    若希望只让管理员看到，需要改 `require_admin` 并同时调整前端导航。
    """
    # 读侧与写侧必须用同一个 owner，走同一个裁决函数（见模块顶部 _kb_owner）
    kb_owner = _kb_owner(user)
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


# ── 切块配置（知识库级，像 Dify 的数据集分段设置） ──
#
# 归属仍由 `_kb_owner` 统一裁决 —— 读侧与写侧必须落到**同一个知识库**，
# 这正是 `_kb_owner` docstring 里记的那次"看到的和改的不是同一个库"的坑。
# 权限口径与 `/files` 对齐：读只要登录（知识库内容本就对所有登录用户开放），
# 写按项目惯例走 `require_admin`。


@router.get("/chunking")
async def get_chunking_config(user: dict = Depends(get_current_user)):
    """
    读取当前知识库的切块配置（登录即可看，口径与 `/api/kb/files` 一致）。

    返回里除了配置本体，还带默认值、合法区间与"改动何时生效"的说明 ——
    让前端不必再抄一份规则，也避免用户误以为保存后立刻重切。
    """
    config = await load_chunking_config(_kb_owner(user))
    return {"success": True, "data": chunking_description(config)}


@router.post("/chunking/preview")
async def preview_chunking_config(
    body: ChunkingPreviewRequest,
    user: dict = Depends(require_admin),
):
    """
    试切预览：按候选配置切一个已入库文件，并**与当前保存的配置对比**。

    为什么需要它：切块参数最大的问题是**用户没法判断它有没有效果** ——
    界面上有个"块大小"输入框，改了到底变没变？原先只能重建索引再肉眼比对。
    现在改完立刻看到"当前 129 块 → 你的配置 214 块"，以及前几块长什么样。

    实测就是靠它发现"自定义分隔符"在结构感知模式下完全无效的：同一个文件
    换两套配置试切，结果一字不差（`identical: true`）。

    只读：解析一次、切两次，不碰数据库与向量库。
    """
    owner_id = _kb_owner(user)
    resolved = await _resolve_readable_file(body.file_id, user)

    current = await load_chunking_config(owner_id)
    try:
        candidate = validate_chunking_payload(
            body.config.model_dump(exclude_unset=True) if body.config else {},
            base=current,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    from core.kb_tasks import preview_split

    upload_dir = str(settings.resolve_path(settings.upload_dir))
    result = await asyncio.to_thread(
        preview_split, str(resolved["path"]), upload_dir, candidate, current
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=409,
            detail=f"这个文件切不了：{result.get('detail') or result.get('reason')}",
        )
    return {"success": True, "data": result}


@router.put("/chunking", response_model=APIResponse)
async def update_chunking_config(
    payload: ChunkingConfigUpdate,
    user: dict = Depends(require_admin),
):
    """
    写入当前知识库的切块配置（仅管理员）。

    只对**之后上传的新文件**即时生效；已入库文件需调用 `POST /api/kb/rebuild`
    才会按新配置重切（见返回体里的 `applies_to`）。
    """
    owner_id = _kb_owner(user)
    current = await load_chunking_config(owner_id)
    try:
        # exclude_unset：只覆盖客户端显式传来的字段，缺省字段沿用当前值（PUT 的部分更新语义）
        config = validate_chunking_payload(payload.model_dump(exclude_unset=True), base=current)
    except ValueError as e:
        # 越界值（如 chunk_size=10 / overlap>=size）明确回 400 + 人话原因
        raise HTTPException(status_code=400, detail=str(e)) from e

    await save_chunking_config(owner_id, config)
    logger.info(
        "[KB] 切块配置更新: owner={} mode={} size={} overlap={} separators={}",
        owner_id,
        config.mode,
        config.chunk_size,
        config.chunk_overlap,
        config.separators,
    )
    return APIResponse(
        success=True,
        message="切块配置已保存：对之后上传的新文件生效，已入库文件需重建索引后生效",
        data=chunking_description(config),
    )


# ── 只读：查看入库切片 / 原文件内容（知识库页的"预览"） ──
#
# 授权口径**跟随 `/files`**：登录即可看。上传 / 删除 / 重建 / 清空才要管理员。
# 这不是新开的口子 —— 知识库内容早就是对所有已登录用户开放的（问答与证据轨
# 都能读到它），预览只是同一份数据的更直接视图。归属仍由 `_kb_owner` 统一裁决。


async def _resolve_readable_file(file_id: int, user: dict) -> dict:
    """
    取文件记录并校验"确实属于这份知识库"，同时把磁盘路径做**包含性校验**。

    Returns:
        ``{record, path}`` —— ``path`` 是校验过的绝对路径。

    Raises:
        HTTPException: 404 记录不存在 / 不属于本知识库；409 源文件不在磁盘上；
            400 路径越界（库里的 filepath 可能来自别的机器，见下）。
    """
    owner_id = _kb_owner(user)
    record = await get_file_by_id(file_id)
    if not record or record["owner_id"] != owner_id:
        raise HTTPException(status_code=404, detail="文件不存在")

    upload_dir = str(settings.resolve_path(settings.upload_dir))
    try:
        # 关键：**按 file_id 查库拿路径，再用包含性校验兜底**，绝不接受客户端传文件名
        # —— 这个项目被那条路咬过一次（`../../../../config/settings.py` 可读写到目录外）。
        path = ensure_within(upload_dir, record["filepath"])
    except ValueError as e:
        # 真实发生过：库里存着**另一台机器**的绝对路径（数据库整体搬过来的），
        # 在 Linux 上它会被当成相对路径解析而越界。这里如实说明，而不是 500。
        logger.warning("[KB] 预览被包含性校验拦下: file_id={} - {}", file_id, e)
        raise HTTPException(
            status_code=400,
            detail="该文件的记录指向一个不在知识库目录内的路径（可能来自其它机器），无法预览",
        ) from e
    return {"record": record, "path": path}


@router.get("/files/{file_id}/chunks")
async def get_file_chunks(
    file_id: int,
    limit: int = 200,
    offset: int = 0,
    user: dict = Depends(get_current_user),
):
    """
    查看某个文件的**入库切片**（知识库页"切片"视图的数据来源）。

    显示的是模型实际读到的东西 —— 分块边界在哪、PDF 页码有没有丢、解析有没有
    把标题重复一遍。这些以前只有模型看得见。
    """
    resolved = await _resolve_readable_file(file_id, user)
    record = resolved["record"]

    from rag.vector_store import list_chunks

    # Chroma 是同步阻塞调用，必须丢到线程里，否则卡住事件循环
    data = await asyncio.to_thread(list_chunks, record["filename"], _kb_owner(user), limit, offset)
    return {
        "success": True,
        "data": {
            "file_id": record["id"],
            "filename": record["filename"],
            "total": data["total"],
            "offset": offset,
            "limit": limit,
            "chunks": data["chunks"],
        },
    }


@router.get("/files/{file_id}/content")
async def get_file_content(
    file_id: int,
    mode: str = "raw",
    user: dict = Depends(get_current_user),
):
    """
    原文件内容。

    - ``mode=raw``（默认）：直接返回原文件。PDF 交给浏览器内置查看器渲染，
      图片按图片显示，文本类按纯文本。
    - ``mode=text``：返回**解析后**的纯文本（``load_document_detailed`` 的结果），
      用来对照"原文"与"入库文本"的差异。
    """
    resolved = await _resolve_readable_file(file_id, user)
    record, path = resolved["record"], resolved["path"]

    if not path.is_file():
        raise HTTPException(status_code=409, detail="源文件不在磁盘上")

    if mode == "text":
        from rag.document_loader import load_document_detailed

        upload_dir = str(settings.resolve_path(settings.upload_dir))
        outcome = await asyncio.to_thread(load_document_detailed, str(path), upload_dir)
        return {
            "success": True,
            "data": {
                "file_id": record["id"],
                "filename": record["filename"],
                "status": outcome.status,
                "detail": outcome.detail,
                "text": outcome.content or "",
                "chars": len(outcome.content or ""),
                # PDF 才有逐页文本（pages[0] = 第 1 页）。有了它，"原文"视图
                # 就能按页对照，也让切片上的 page 字段有了出处。
                "pages": outcome.pages or [],
            },
        }

    media_type = mimetypes.guess_type(record["filename"])[0] or "application/octet-stream"
    return FileResponse(str(path), media_type=media_type, filename=record["filename"])


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

    owner_id = _kb_owner(user)
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
    task = await get_upload_task(task_id, _kb_owner(user))
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
    owner_id = _kb_owner(user)
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
    owner_id = _kb_owner(user)
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
    owner_id = _kb_owner(user)
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
