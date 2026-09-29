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


def process_file_sync(file_path: str, filename: str, owner_id: int) -> dict:
    """
    处理单个文件：解析 → 分块 → 向量化 → 落库。

    **同步函数**，应在工作线程中执行。
    """
    from rag.document_loader import load_document_detailed
    from rag.text_splitter import process_documents_batch
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

    docs = [
        {"filepath": outcome.filepath, "filename": outcome.filename, "content": outcome.content}
    ]
    with span(
        "document.split",
        filename=filename,
        semantic_splitter=settings.use_semantic_splitter,
    ):
        processed = process_documents_batch(
            docs, use_semantic_splitter=settings.use_semantic_splitter
        )
    if not processed or not processed[0].get("chunks"):
        return {"success": False, "error": "文件无可索引内容（分块后没有可用文本）"}

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
    from rag.text_splitter import process_documents_batch
    from rag.vector_store import add_documents, delete_all_by_owner, reset_vector_store

    delete_all_by_owner(owner_id)
    reset_vector_store()

    files = run_async_from_thread(get_files_by_owner(owner_id))
    upload_dir = str(settings.resolve_path(settings.upload_dir))

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
            processed = process_documents_batch(
                [{"filepath": filepath, "filename": filename, "content": outcome.content}],
                use_semantic_splitter=settings.use_semantic_splitter,
            )
        except Exception as e:
            failed_files.append({"filename": filename, "reason": "split_error", "detail": str(e)})
            logger.error("[KB] 重建分块失败: {} - {}", filename, e)
            continue

        if not processed or not processed[0].get("chunks"):
            empty_files.append(
                {"filename": filename, "reason": "no_chunks", "detail": "分块后没有可用文本"}
            )
            logger.warning("[KB] 重建无分块(跳过): {}", filename)
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
        try:
            add_documents(chunks, metadatas)
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
