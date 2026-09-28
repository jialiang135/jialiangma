"""
知识库管理 Agent —— 文档上传、知识库维护、向量库管理

这些节点是 async 的（由 LangGraph 调用），但底下的解析/分块/向量化都是
CPU/IO 密集的同步工作 —— 统一丢到线程池（``asyncio.to_thread``）执行，
避免阻塞事件循环。重活的具体实现收在 ``core/kb_tasks``，与上传接口共用一份。
"""
import asyncio
import os
from pathlib import Path
from langchain_core.messages import AIMessage
from loguru import logger

from agent.state import AgentState
from config.settings import settings
from core.kb_tasks import process_file_sync, rebuild_knowledge_base
from core.database import (
    get_files_by_owner,
    get_file_by_id,
    delete_file_record,
    delete_all_file_records,
)
from core.paths import remove_within


async def manage_agent_node(state: AgentState) -> dict:
    """
    知识库管理 Agent 节点。
    根据 operation 字段执行不同的管理操作：
    - upload: 处理上传的文件
    - delete: 删除指定文件
    - list: 列出所有文件
    - rebuild: 重建知识库
    """
    operation = state.get("operation", "list")
    owner_id = state.get("owner_id", 1)
    logger.info("[ManageAgent] 执行操作: {}, owner_id={}", operation, owner_id)

    result_message = ""
    reasoning = []

    try:
        if operation == "upload":
            result_message, reasoning = await _handle_upload(state, owner_id)
        elif operation == "delete":
            result_message, reasoning = await _handle_delete(state, owner_id)
        elif operation == "rebuild":
            result_message, reasoning = await _handle_rebuild(state, owner_id)
        elif operation == "clear":
            result_message, reasoning = await _handle_clear(owner_id)
        else:
            result_message, reasoning = await _handle_list(owner_id)

    except Exception as e:
        logger.error(f"[ManageAgent] 操作失败: {e}")
        result_message = f"❌ 操作失败: {str(e)}"
        reasoning = [f"❌ 错误: {str(e)[:200]}"]

    return {
        "final_answer": result_message,
        "operation_result": result_message,
        "reasoning_log": reasoning,
        "messages": [AIMessage(content=result_message)],
    }


async def _handle_upload(state: AgentState, owner_id: int) -> tuple[str, list]:
    """处理文件上传入库"""
    upload_files = state.get("upload_files", [])
    if not upload_files:
        return "未选择任何文件。", ["⚠️ 未选择文件"]

    reasoning = []
    total_chunks = 0
    success_files = []

    for filepath in upload_files:
        if not os.path.exists(filepath):
            reasoning.append(f"⚠️ 文件不存在: {filepath}")
            continue

        filename = Path(filepath).name
        file_size = os.path.getsize(filepath)
        reasoning.append(f"📄 处理文件: {filename} ({file_size/1024:.1f}KB)")

        try:
            # 解析 → 分块 → 向量化 → 落库：复用 core/kb_tasks 的同一份实现，
            # 丢线程池执行（CPU/IO 密集；其内部 DB 调用会提交回主循环）
            result = await asyncio.to_thread(
                process_file_sync, filepath, filename, owner_id
            )
            if result.get("success"):
                if result.get("skipped"):
                    reasoning.append(f"⏭️ {result.get('message', '已存在，跳过')}")
                else:
                    chunk_n = result.get("chunks", 0)
                    total_chunks += chunk_n
                    success_files.append(filename)
                    reasoning.append(f"✅ {filename} 入库完成 ({chunk_n} 块)")
            else:
                reasoning.append(f"❌ {filename} 处理失败: {result.get('error')}")

        except Exception as e:
            reasoning.append(f"❌ {filename} 处理失败: {str(e)[:200]}")
            logger.error(f"[ManageAgent] 文件处理失败: {filename} - {e}")

    result = (
        f"## 知识库入库结果\n\n"
        f"- 成功: {len(success_files)} 个文件\n"
        f"- 总文本块: {total_chunks} 块\n"
        f"- 文件列表: {', '.join(success_files) if success_files else '无'}\n\n"
        f"现在可以在「对话问答」中测试知识库检索效果。"
    )
    return result, reasoning


async def _handle_list(owner_id: int) -> tuple[str, list]:
    """列出当前用户的所有文件"""
    files = await get_files_by_owner(owner_id)
    from rag.vector_store import get_collection_stats
    stats = await asyncio.to_thread(get_collection_stats, owner_id)

    if not files:
        return (
            "## 知识库为空\n\n还没有上传任何文档。"
            "请在「知识库管理」页面上传个人简历、项目文档、技术笔记等资料。",
            ["📭 知识库为空"],
        )

    lines = [
        f"## 知识库概况",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 文件数 | {len(files)} |",
        f"| 向量块总数 | {stats['total_chunks']} |",
        f"| 涉及文件 | {stats['unique_files']} |",
        "",
        "## 文件明细",
    ]

    reasoning = [f"📋 知识库: {len(files)} 个文件, {stats['total_chunks']} 个向量块"]

    for f in files:
        size_kb = f.get("file_size", 0) / 1024
        lines.append(
            f"- **{f['filename']}** | "
            f"ID: {f['id']} | "
            f"{size_kb:.1f}KB | "
            f"{f.get('chunk_count', 0)} 块 | "
            f"{f.get('created_at', '')}"
        )

    return "\n".join(lines), reasoning


async def _handle_delete(state: AgentState, owner_id: int) -> tuple[str, list]:
    """删除指定文件"""
    # 从 user_query 中提取文件ID
    user_query = state.get("user_query", "")
    reasoning = []

    # 尝试从查询中解析 file_id
    try:
        # 尝试直接解析数字
        import re
        ids = re.findall(r'\b(\d+)\b', user_query)
        if not ids:
            return "请指定要删除的文件ID。用法: 删除文件 ID=123", ["⚠️ 未指定文件ID"]

        file_id = int(ids[0])
    except (ValueError, IndexError):
        return "无法解析文件ID，请使用「删除文件 ID=XXX」格式。", ["⚠️ 无法解析文件ID"]

    file_record = await get_file_by_id(file_id)
    if not file_record:
        return f"文件 ID={file_id} 不存在。", [f"⚠️ 文件 ID={file_id} 不存在"]

    if file_record["owner_id"] != owner_id:
        return "无权删除此文件。", ["🚫 越权操作被拦截"]

    filename = file_record["filename"]

    # 从向量库删除（Chroma 是同步的，丢线程池）
    from rag.vector_store import delete_by_file
    deleted_chunks = await asyncio.to_thread(delete_by_file, filename, owner_id)

    # 从文件系统删除（走 remove_within，库里的历史路径可能越界）
    upload_dir = str(settings.resolve_path(settings.upload_dir))
    filepath = file_record["filepath"]
    if not remove_within(upload_dir, filepath):
        logger.warning("[ManageAgent] 磁盘文件未删除（越界或不存在）: {}", filepath)

    # 从数据库删除
    await delete_file_record(file_id, owner_id)

    reasoning.append(f"🗑️ 已删除: {filename} ({deleted_chunks} 个向量块)")

    return (
        f"## 删除成功\n\n"
        f"- 文件: {filename}\n"
        f"- 移除向量块: {deleted_chunks} 个\n"
        f"- 磁盘文件已清理",
        reasoning,
    )


async def _handle_clear(owner_id: int) -> tuple[str, list]:
    """清空知识库"""
    from rag.vector_store import delete_all_by_owner, reset_vector_store

    deleted = await asyncio.to_thread(delete_all_by_owner, owner_id)
    count = await delete_all_file_records(owner_id)

    # 清理上传文件目录（同步文件 IO，丢线程池）
    upload_dir = settings.resolve_path(settings.upload_dir)

    def _clear_dir() -> None:
        if not upload_dir.exists():
            return
        for entry in upload_dir.iterdir():
            if entry.is_file():
                try:
                    entry.unlink()
                except OSError as e:
                    logger.warning("清理文件失败: {} - {}", entry, e)

    await asyncio.to_thread(_clear_dir)
    await asyncio.to_thread(reset_vector_store)

    return (
        f"## 知识库已清空\n\n"
        f"- 删除向量块: {deleted} 个\n"
        f"- 删除文件记录: {count} 条\n"
        f"- 磁盘文件已清理\n\n"
        f"可以重新上传文件构建知识库。",
        [f"🧹 知识库已清空: {deleted} 向量块, {count} 文件记录"],
    )


async def _handle_rebuild(state: AgentState, owner_id: int) -> tuple[str, list]:
    """
    重建知识库：先清空向量库，再按数据库中的文件记录重新入库。

    整个重建过程（可能数分钟到数小时）丢线程池执行 —— 绝不能出现在事件循环里。
    具体实现在 core/kb_tasks，与 /api/kb/rebuild 共用同一份。
    """
    files = await get_files_by_owner(owner_id)
    if not files:
        return "知识库中没有文件，无需重建。请先上传文档。", ["📭 知识库为空，无需重建"]

    reasoning = ["🔄 开始重建知识库...", f"📁 待处理文件: {len(files)} 个"]
    total_chunks = await asyncio.to_thread(rebuild_knowledge_base, owner_id)
    reasoning.append(f"✅ 重建完成: {total_chunks} 个向量块")

    return (
        f"## 知识库重建完成\n\n"
        f"- 处理文件: {len(files)} 个\n"
        f"- 总向量块: {total_chunks} 块",
        reasoning,
    )
