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
import contextvars
import hashlib
import os
import shutil

from loguru import logger

from config.settings import settings
from core.chunking import ChunkingConfig, resolve_chunking_config
from core.db.engine import run_async_from_thread
from core.db.files import (
    find_file_by_hash_or_name,
    get_files_by_owner,
    insert_file_record,
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


# kb_tasks 自己产生的原因码（rag/document_loader 之外的入库/分块阶段）。
_PIPELINE_REASON_LABELS = {
    "split_error": "分块失败",
    "embed_error": "向量化失败",
    "no_chunks": "分块后没有可用文本",
}
# 这些原因光有标签不够，还要把细节带上（否则"失败"了但不知失败在哪）。
_DETAIL_REASONS = {"parse_error", "split_error", "embed_error", "zip_limit_exceeded"}


def _problem_label(reason: str, detail: str = "") -> str:
    """
    把原因码（+细节）转成一句人话，用于任务状态与错误信息。

    标签统一来自 ``rag.document_loader.REASON_LABELS``，避免在调用方各写一份、
    日后漂移；detail 只在"标签说不清"的原因上附带。
    """
    from rag.document_loader import REASON_LABELS

    label = _PIPELINE_REASON_LABELS.get(reason) or REASON_LABELS.get(reason) or reason
    detail = (detail or "").strip()
    if reason in _DETAIL_REASONS and detail:
        return f"{label}: {detail[:80]}"
    return label


# 当前生效的切块配置 —— 由入库 / 重建入口设置，``_split_loaded_document`` 读取。
#
# **为什么用 contextvar，而不是给 _split_loaded_document 加形参**：
# 该函数是既有回归测试的打桩点，测试按 ``(outcome, semantic)`` 两个位置参数把它
# 换掉（见 tests/test_silent_failures.py 的重建用例）。一旦加形参，这些桩会直接
# 报 "unexpected keyword argument" —— 等于为了新功能打爆一堆旧用例。
# contextvar 按线程 / 上下文隔离、不改调用签名；直接调用它的既有测试读不到值，
# 自然回落到默认配置（= 改造前行为）。这不是新发明的套路：``core/eval_runner.py``
# 的检索参数覆盖用的就是同一手法，理由也一样（不污染全局 settings）。
_CURRENT_CHUNKING: contextvars.ContextVar[ChunkingConfig | None] = contextvars.ContextVar(
    "kb_chunking_config", default=None
)


def _current_chunking() -> ChunkingConfig:
    """取当前上下文里的切块配置；未显式设置时用默认值（等价改造前行为）。"""
    config = _CURRENT_CHUNKING.get()
    if config is not None:
        return config
    from core.chunking import default_chunking_config

    return default_chunking_config()


@contextlib.contextmanager
def applied_chunking_config(config: ChunkingConfig):
    """在上下文里临时启用一套切块配置（退出时务必 reset，线程池会复用线程）。"""
    token = _CURRENT_CHUNKING.set(config)
    try:
        yield
    finally:
        _CURRENT_CHUNKING.reset(token)


def _split_loaded_document(outcome, use_semantic_splitter: bool) -> list[dict]:
    """
    把加载结果切成 chunk 列表，并**尽量为每个 chunk 记录来源页码**。

    Returns:
        ``[{"content": str, "page": int | None}, ...]``

    PDF 页码策略（务实做法：**按页分别切分**）
    ----------------------------------------
    PDF 加载时保留了逐页文本（``outcome.pages``，见 ``rag/document_loader.py``）。
    这里**逐页调用分块器**，再把该页产出的所有 chunk 标记为这一页的页码。
    精度：每个 chunk 都能精确对应到它所在的**物理页**（1-based）。

    局限（如实说明，别夸大）：
    - chunk **不会跨页**。原先"整篇一起切"时，一个自然段若横跨两页边界，
      会被切进同一个 chunk；现在它在页边界处被切成两个 chunk（各属其页）。
      对"引用回溯到页"来说这是**更准确**的取舍，代价是跨页语义单元被拆开。
    - 只有 PDF 有页码；其它格式 ``page`` 为 ``None``（行为与旧版一致）。
    - 页码来自 PDF 的文字层（pypdf ``extract_text``）。纯扫描件没有文字层 →
      本来就取不到文本，也就谈不上页码（需 OCR，当前不产出页码）。

    切块参数（size / overlap / 分隔符）来自**当前上下文里的知识库配置**
    （``applied_chunking_config`` 设置，默认回落到全局 settings），
    所以同一个函数既能服务默认知识库，也能服务自定义过切块参数的知识库。
    """
    from rag.text_splitter import process_document, process_documents_batch

    config = _current_chunking()
    pages = getattr(outcome, "pages", None) or []
    if pages:
        # PDF：逐页切分。**每页单独调用** process_document（无法复用批量入口，
        # 因为要按页拿到"页码 → 该页的块"的对应关系）。
        chunks: list[dict] = []
        for page_no, page_text in enumerate(pages, start=1):
            if not page_text or not page_text.strip():
                continue
            for text in process_document(
                page_text,
                chunk_size=config.chunk_size,
                chunk_overlap=config.chunk_overlap,
                separators=config.separators,
                use_semantic_splitter=use_semantic_splitter,
            ):
                chunks.append({"content": text, "page": page_no})
        return chunks

    # 非 PDF（无逐页信息）：走批量入口整体切分，页码为 None。
    # 用 process_documents_batch 而不是 process_document，是为了保持与旧实现
    # 相同的调用路径（含"单文档分块后为空"的既有行为）。
    processed = process_documents_batch(
        [{"filepath": outcome.filepath, "filename": outcome.filename, "content": outcome.content}],
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
        separators=config.separators,
        use_semantic_splitter=use_semantic_splitter,
    )
    if not processed or not processed[0].get("chunks"):
        return []
    return [{"content": text, "page": None} for text in processed[0]["chunks"]]


def _chunk_metadatas(chunks: list[dict], owner_id: int, filename: str, filepath: str) -> list[dict]:
    """
    由 chunk 列表构造写入向量库的 metadata（``page`` 仅在可得时写入）。

    保持既有键不变（``owner_id`` / ``source`` / ``chunk_idx`` / ``filepath``）——
    ``page`` 是**新增**键，仅 PDF 有值；Chroma 不接受 ``None`` 元数据值，故缺失时不写。
    """
    metadatas = []
    for i, ch in enumerate(chunks):
        meta = {"owner_id": owner_id, "source": filename, "chunk_idx": i, "filepath": filepath}
        if ch["page"] is not None:
            meta["page"] = ch["page"]
        metadatas.append(meta)
    return metadatas


def process_file_sync(file_path: str, filename: str, owner_id: int) -> dict:
    """
    处理单个文件：解析 → 分块 → 向量化 → 落库。

    **同步函数**，应在工作线程中执行。
    """
    from rag.document_loader import load_document_detailed
    from rag.vector_store import add_documents

    file_size = os.path.getsize(file_path)
    upload_dir = str(settings.resolve_path(settings.upload_dir))

    # 解析（PDF/OCR 可能是分钟级）与分块各记一个 span，便于定位慢在哪一步
    with span("document.parse", filename=filename, file_size=file_size):
        outcome = load_document_detailed(file_path, upload_dir)
    # 失败原因必须透出：以前对"引擎缺失 / 扫描件无文字层 / 文件本来就空"
    # 三种原因都只回一句"无法解析文件内容"，用户与日志都看不出所以然。
    if outcome.status == "failed":
        return {
            "success": False,
            "error": f"无法解析文件内容（{_problem_label(outcome.reason, outcome.detail)}）",
        }
    if outcome.status == "empty":
        return {
            "success": False,
            "error": f"文件无可索引内容（{_problem_label(outcome.reason, outcome.detail)}）",
        }

    # 取该知识库当前生效的切块配置（读取失败会回落到默认，不阻断入库）。
    # 必须在这里取、而不是进 _split_loaded_document 里取：后者不知道 owner_id。
    chunking = resolve_chunking_config(owner_id)
    with (
        span("document.split", filename=filename, semantic_splitter=chunking.use_semantic_splitter),
        # 按页切分（PDF 时每块带页码），见 _split_loaded_document 的精度说明
        applied_chunking_config(chunking),
    ):
        chunks = _split_loaded_document(outcome, chunking.use_semantic_splitter)
    if not chunks:
        return {"success": False, "error": "文件无可索引内容（分块后没有可用文本）"}

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

    metadatas = _chunk_metadatas(chunks, owner_id, filename, dest_path)
    # 向量化是最慢的一步（每批一次 DashScope HTTP），单独计时
    with span("document.embed", filename=filename, chunks=len(chunks)):
        add_documents([c["content"] for c in chunks], metadatas)

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


# ============================================================
# 重建任务的状态语义（前端轮询 / UI 依赖这几个值，别改含义）
# ============================================================
#   done    —— 所有待重建文件都成功入库，未出现解析失败或空内容；
#              知识库与文件表一致。（**无任何文件时也是 done**：
#              "空知识库"是合法状态，不是失败。）
#   partial —— 至少一个文件成功入库，但存在解析失败 / 内容为空的文件。
#              知识库可用但**不完整**；error 字段列出受影响文件及原因。
#   failed  —— 一个向量块都没写进去，但确实有待重建的文件
#              （全部失败 / 全部为空）。error 字段说明原因，
#              前端应提示用户检查文件或 OCR 环境。
#
# 为什么不再无条件写 done：早期实现对每个文件的失败都只 `continue`，
# 循环结束后照样写 `status="done"`。于是**全部文件解析失败时界面显示
# "重建完成 0 块"**，没有任何 failed —— 用户以为重建成功，实际知识库是空的。
REBUILD_STATUS_DONE = "done"
REBUILD_STATUS_PARTIAL = "partial"
REBUILD_STATUS_FAILED = "failed"

#: 任务 error 里最多逐条列出的问题文件数（超出用"等共 N 个"概括）。
_MAX_LISTED_PROBLEMS = 8


def _describe_problem(item: dict) -> str:
    """把一条问题记录渲染成 ``文件名 [原因]``。"""
    return f"{item['filename']} [{_problem_label(item['reason'], item.get('detail', ''))}]"


def _format_file_problems(failed_files: list[dict], empty_files: list[dict]) -> str:
    """把失败 / 无内容文件汇总成一句可读的原因说明（含文件名 + 简短原因）。"""
    parts = []
    for label, items in (("解析失败", failed_files), ("无内容", empty_files)):
        if not items:
            continue
        shown = "；".join(_describe_problem(it) for it in items[:_MAX_LISTED_PROBLEMS])
        if len(items) > _MAX_LISTED_PROBLEMS:
            shown += f"；等共 {len(items)} 个"
        parts.append(f"{label} {len(items)} 个: {shown}")
    return " | ".join(parts)


def _summarize_rebuild(report: dict) -> tuple[str, str]:
    """
    由重建报告推导 ``(status, error)``。**纯函数**，便于单测。

    规则（对应上面的状态语义）：
        - 无文件 → done（空知识库合法）；
        - 有文件但 0 块入库 → failed；
        - 有块入库但存在失败 / 空内容文件 → partial；
        - 其余 → done。
    """
    total_files = report["total_files"]
    total_chunks = report["total_chunks"]
    failed_files = report["failed_files"]
    empty_files = report["empty_files"]

    # 预检没过 → 中止，**未动旧索引**。这与"跑到一半失败"是两回事，必须说清楚：
    # 前者知识库还是完整的，后者可能已经残缺。
    if report.get("aborted"):
        return REBUILD_STATUS_FAILED, (
            f"重建已中止（现有索引未改动）：{total_files} 个文件中有 "
            f"{len(failed_files)} 个源文件在磁盘上找不到。"
            f"{_format_file_problems(failed_files, [])}"
        )[:1000]

    if total_files == 0:
        return REBUILD_STATUS_DONE, ""

    if total_chunks == 0:
        status = REBUILD_STATUS_FAILED
    elif failed_files or empty_files:
        status = REBUILD_STATUS_PARTIAL
    else:
        status = REBUILD_STATUS_DONE

    if status == REBUILD_STATUS_DONE:
        return status, ""

    header = f"成功 {report['succeeded_files']}/{total_files} 个文件，共 {total_chunks} 块"
    prefix = "重建失败" if status == REBUILD_STATUS_FAILED else "重建部分成功"
    error = f"{prefix}：{header}。{_format_file_problems(failed_files, empty_files)}"
    return status, error[:1000]


def _rebuild_knowledge_base_report(owner_id: int) -> dict:
    """
    重建整个知识库，返回**带失败明细的报告**（同步，应在工作线程执行）。

    Returns:
        ``{total_chunks, total_files, succeeded_files, failed_files, empty_files}``；
        后两者是 ``[{"filename", "reason", "detail"}, ...]``。
    """
    from rag.document_loader import load_document_detailed
    from rag.vector_store import add_documents, delete_all_by_owner, reset_vector_store

    files = run_async_from_thread(get_files_by_owner(owner_id))
    upload_dir = str(settings.resolve_path(settings.upload_dir))

    # ── 预检：文件必须都能找到，才允许动旧索引 ──
    #
    # 重建是**先删后建**，而删除不可逆。若不预检，一旦有文件读不到，重建会把
    # 知识库留成**残缺**状态。实测踩过：14 条文件记录里有 9 条的 filepath 指向
    # 另一个机器上的绝对路径（数据库是从开发机整体搬过来的），于是重建完只剩
    # 5 个文件 / 164 块，原有的 289 块全没了 —— 数据静默损失，而任务状态只是
    # 一个 "partial"，不细看根本发现不了。
    #
    # 为什么只查"存在性"、不整个解析一遍：解析要跑 PDF/OCR，代价与重建本身相当，
    # 等于白做一遍。而"路径不对/文件被删"是这一层唯一能廉价拦住的失效，也正是
    # 实际发生的那个。解析层面的失败仍由下面的失败清单如实报告。
    missing = [f for f in files if not os.path.isfile(f["filepath"])]
    if missing:
        logger.error(
            "[KB] 重建中止：{}/{} 个文件的源文件不存在，现有索引未改动",
            len(missing),
            len(files),
        )
        return {
            "total_chunks": 0,
            "total_files": len(files),
            "succeeded_files": 0,
            "failed_files": [
                {
                    "filename": f["filename"],
                    "reason": "file_missing",
                    "detail": f"源文件不存在，已中止重建（现有索引未改动）: {f['filepath']}",
                }
                for f in missing
            ],
            "empty_files": [],
            "aborted": True,
        }

    delete_all_by_owner(owner_id)
    reset_vector_store()

    # 重建按"当前配置"重切：这正是"改了切块参数后对已有文件生效"的唯一入口。
    # 在循环前取一次（重建期间配置不变，避免每个文件都查一次库）。
    chunking = resolve_chunking_config(owner_id)

    total_chunks = 0
    succeeded = 0
    failed_files: list[dict] = []
    empty_files: list[dict] = []

    for file_record in files:
        filepath = file_record["filepath"]
        filename = file_record["filename"]

        # 解析：区分「失败」（引擎缺失/不存在/不支持/异常）与「为空」（没文字）。
        outcome = load_document_detailed(filepath, upload_dir)
        if outcome.status == "failed":
            failed_files.append(
                {"filename": filename, "reason": outcome.reason, "detail": outcome.detail}
            )
            logger.warning(
                "[KB] 重建失败(跳过): {} - {} [{}]", filename, outcome.detail, outcome.reason
            )
            continue
        if outcome.status == "empty":
            empty_files.append(
                {"filename": filename, "reason": outcome.reason, "detail": outcome.detail}
            )
            logger.warning(
                "[KB] 重建无内容(跳过): {} - {} [{}]", filename, outcome.detail, outcome.reason
            )
            continue

        try:
            # 按页切分（PDF 时每块带页码），见 _split_loaded_document 的精度说明
            with applied_chunking_config(chunking):
                chunks = _split_loaded_document(outcome, chunking.use_semantic_splitter)
        except Exception as e:
            failed_files.append({"filename": filename, "reason": "split_error", "detail": str(e)})
            logger.error("[KB] 重建分块失败: {} - {}", filename, e)
            continue

        if not chunks:
            empty_files.append(
                {"filename": filename, "reason": "no_chunks", "detail": "分块后没有可用文本"}
            )
            logger.warning("[KB] 重建无分块(跳过): {}", filename)
            continue

        metadatas = _chunk_metadatas(chunks, owner_id, filename, filepath)
        try:
            add_documents([c["content"] for c in chunks], metadatas)
            run_async_from_thread(update_file_chunk_count(file_record["id"], len(chunks)))
        except Exception as e:
            failed_files.append({"filename": filename, "reason": "embed_error", "detail": str(e)})
            logger.error("[KB] 重建向量化/入库失败: {} - {}", filename, e)
            continue

        total_chunks += len(chunks)
        succeeded += 1

    return {
        "total_chunks": total_chunks,
        "total_files": len(files),
        "succeeded_files": succeeded,
        "failed_files": failed_files,
        "empty_files": empty_files,
    }


def rebuild_knowledge_base(owner_id: int) -> int:
    """
    重建整个知识库，返回总向量块数。

    **同步函数**，应在工作线程中执行 —— 这个操作可能耗时数分钟到数小时
    （PDF 解析 + OCR + 逐块向量化），绝不能进事件循环。

    保留返回 ``int`` 的签名（``agent/manage_agent.py`` 依赖它）；需要失败明细
    时用 ``_rebuild_knowledge_base_report``。存在未成功文件时会打 WARNING，
    保证即使走的是只认 int 的旧调用方，日志里也能看到"重建并非全成功"。
    """
    report = _rebuild_knowledge_base_report(owner_id)
    if report["failed_files"] or report["empty_files"]:
        logger.warning(
            "[KB] 重建存在未成功文件: 成功 {}/{}，失败 {}，无内容 {}",
            report["succeeded_files"],
            report["total_files"],
            len(report["failed_files"]),
            len(report["empty_files"]),
        )
    return report["total_chunks"]


def rebuild_task(task_id: str, owner_id: int) -> None:
    """重建任务包装：更新 upload_tasks 状态。同步，线程池执行。"""
    try:
        run_async_from_thread(update_upload_task(task_id, status="processing", progress=10))
        report = _rebuild_knowledge_base_report(owner_id)
        status, error = _summarize_rebuild(report)
        run_async_from_thread(
            update_upload_task(
                task_id,
                status=status,
                progress=100,
                chunk_count=report["total_chunks"],
                error=error,
            )
        )
        logger.info(
            "[KB] 重建结束: status={} 成功 {}/{} 文件, {} 块; {}",
            status,
            report["succeeded_files"],
            report["total_files"],
            report["total_chunks"],
            error or "无异常",
        )
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


# ============================================================
# 知识库一致性自检
# ============================================================
#
# 为什么需要这个
# --------------
# 「文件表(files)」与「向量库(Chroma)」是两处独立存储，会各自漂移。
# 危险的地方在于**漂移时不报错**：表现是界面显示「知识库为空」，
# 但模型却能引用某份文档来回答 —— 用户看到的是一个自信的错误答案。
#
# 本项目真实踩过一次：早期跑测试把测试夹具的文档灌进了向量库，
# 清理时只删了数据库行与磁盘文件、**漏了向量库**，于是：
#   files 表 0 条 / 向量库 1 条
# 问「介绍一下你自己」时模型回答「我是张三，AI 工程师」（测试夹具内容）。
#
# 因此提供两个函数：一个是检查，一个是清理孤儿块。


def check_kb_consistency(owner_id: int = 1) -> dict:
    """
    比对文件表与向量库，返回不一致清单。

    **同步函数**，应在工作线程中调用（内部会通过 run_async_from_thread 读库）。

    Returns:
        ``{consistent, db_file_count, vector_chunk_count, orphan_sources,
           sources_without_chunks, ...}``
        - ``orphan_sources``：向量库里有、文件表里没有 —— **最危险的一类**，
          会让"空知识库"仍然能检索出内容
        - ``sources_without_chunks``：文件表里有、向量库里没有 ——
          文件记录了但检索不到（通常是入库中断）
    """
    from rag.vector_store import get_collection_stats

    files = run_async_from_thread(get_files_by_owner(owner_id))
    db_sources = {f["filename"] for f in files if f.get("filename")}
    stats = get_collection_stats(owner_id)
    vec_sources = set(stats.get("files") or [])

    orphan_sources = sorted(vec_sources - db_sources)
    sources_without_chunks = sorted(db_sources - vec_sources)

    report = {
        "owner_id": owner_id,
        "consistent": not orphan_sources and not sources_without_chunks,
        "db_file_count": len(db_sources),
        "vector_chunk_count": stats.get("total_chunks", 0),
        "vector_source_count": len(vec_sources),
        "orphan_sources": orphan_sources,
        "sources_without_chunks": sources_without_chunks,
    }
    if not report["consistent"]:
        logger.warning(
            "[KB] 知识库不一致: 向量库孤儿来源={} 缺向量的文件={}",
            orphan_sources,
            sources_without_chunks,
        )
    return report


def repair_kb_consistency(owner_id: int = 1) -> dict:
    """
    清理「向量库有、文件表没有」的孤儿块。

    只清孤儿块（那才是导致"空知识库仍能回答"的原因）；
    「文件表有、向量库没有」不动 —— 那通常只需重新入库即可恢复，
    自动删除会丢掉用户的书目记录。

    **同步函数**，应在工作线程中调用。
    """
    from rag.vector_store import delete_by_file

    report = check_kb_consistency(owner_id)
    removed = {}
    for source in report["orphan_sources"]:
        removed[source] = delete_by_file(source, owner_id)
    if removed:
        logger.info("[KB] 已清理孤儿向量块: {}", removed)
    return {
        "removed_sources": removed,
        "removed_chunks": sum(removed.values()),
        "before": report,
    }
