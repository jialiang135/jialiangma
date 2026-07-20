"""
知识库管理 Agent —— 文档上传、知识库维护、向量库管理
"""
import os
import shutil
from pathlib import Path
from langchain_core.messages import AIMessage
from loguru import logger

from agent.state import AgentState
from config.settings import settings
from rag.document_loader import load_documents_from_paths
from rag.text_splitter import process_documents_batch
from rag.vector_store import (
    add_documents,
    delete_by_file,
    delete_all_by_owner,
    get_collection_stats,
    reset_vector_store,
)
from core.database import (
    insert_file_record,
    get_files_by_owner,
    get_file_by_id,
    delete_file_record,
    delete_all_file_records,
    update_file_chunk_count,
)


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

    upload_dir = str(settings.resolve_path(settings.upload_dir))

    for filepath in upload_files:
        if not os.path.exists(filepath):
            reasoning.append(f"⚠️ 文件不存在: {filepath}")
            continue

        filename = Path(filepath).name
        file_size = os.path.getsize(filepath)
        reasoning.append(f"📄 处理文件: {filename} ({file_size/1024:.1f}KB)")

        try:
            # Step 1: 解析文档
            docs = load_documents_from_paths([filepath], upload_dir)
            if not docs:
                reasoning.append(f"⚠️ 无法解析文件内容: {filename}")
                continue

            # Step 2: 文本分块
            processed = process_documents_batch(docs)
            if not processed or not processed[0].get("chunks"):
                reasoning.append(f"⚠️ 文件内容为空: {filename}")
                continue

            chunks = processed[0]["chunks"]
            reasoning.append(f"✂️ 文本分块: {len(chunks)} 块")

            # Step 3: 复制文件到持久化目录
            dest_path = os.path.join(upload_dir, filename)
            if filepath != dest_path:
                shutil.copy2(filepath, dest_path)

            # Step 4: 向量化入库
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

            # Step 5: 写数据库记录
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
            reasoning.append(f"✅ {filename} 入库完成 ({len(chunks)} 块)")

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
    files = get_files_by_owner(owner_id)
    stats = get_collection_stats(owner_id)

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

    file_record = get_file_by_id(file_id)
    if not file_record:
        return f"文件 ID={file_id} 不存在。", [f"⚠️ 文件 ID={file_id} 不存在"]

    if file_record["owner_id"] != owner_id:
        return "无权删除此文件。", ["🚫 越权操作被拦截"]

    filename = file_record["filename"]

    # 从向量库删除
    deleted_chunks = delete_by_file(filename, owner_id)

    # 从文件系统删除
    filepath = file_record["filepath"]
    if os.path.exists(filepath):
        os.remove(filepath)

    # 从数据库删除
    delete_file_record(file_id, owner_id)

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
    deleted = delete_all_by_owner(owner_id)
    count = delete_all_file_records(owner_id)

    # 清理上传文件目录
    upload_dir = str(settings.resolve_path(settings.upload_dir))
    for f in os.listdir(upload_dir):
        fpath = os.path.join(upload_dir, f)
        if os.path.isfile(fpath):
            os.remove(fpath)

    reset_vector_store()

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
    重建知识库：重新处理所有已上传文件。
    先清空向量库，再从数据库中的文件记录重新入库。
    """
    reasoning = ["🔄 开始重建知识库..."]

    # 清空向量库
    delete_all_by_owner(owner_id)
    reset_vector_store()

    # 获取所有文件记录
    files = get_files_by_owner(owner_id)
    if not files:
        return "知识库中没有文件，无需重建。请先上传文档。", ["📭 知识库为空，无需重建"]

    total_chunks = 0
    upload_dir = str(settings.resolve_path(settings.upload_dir))

    for file_record in files:
        filepath = file_record["filepath"]
        filename = file_record["filename"]

        if not os.path.exists(filepath):
            reasoning.append(f"⚠️ 文件不存在，跳过: {filename}")
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
            reasoning.append(f"✅ {filename}: {len(chunks)} 块")

        except Exception as e:
            reasoning.append(f"❌ {filename}: {str(e)[:100]}")

    return (
        f"## 知识库重建完成\n\n"
        f"- 处理文件: {len(files)} 个\n"
        f"- 总向量块: {total_chunks} 块",
        reasoning,
    )
