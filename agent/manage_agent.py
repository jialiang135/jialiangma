"""
知识库管理节点 —— 汇报知识库概况
==================================

这是一个**确定性节点**：不发 LLM、不解析意图，只按 owner 把知识库现状整理成
一份可读的概况（文件数 / 向量块数 / 文件明细）。

关于"为什么只剩这一件事"
------------------------
这个节点原本还带 `upload` / `delete` / `rebuild` / `clear` 四个分支，按
``state["operation"]`` 分发。但 ``operation`` 全项目**只被置为 None**
（``api/sse_stream._initial_state``），没有任何地方把它设成那四个值 ——
四个分支**永远走不到**，是早期"用聊天来管理知识库"那条路的遗留。

那条路本身不成立：聊天里没有文件可传，而"删/清空"这类破坏性操作让模型去解析
用户输入再执行，风险远大于收益。所以管理功能现在完整地由 REST 路由提供
（``api/routes/kb_routes.py``），重活收在 ``core/kb_tasks.py``，与本节点无关。

按"做不成就删掉、别留半成品"的原则，四个不可达分支连同它们依赖的 import
一并删除；保留 ``_handle_list`` —— 它是**真能用**的：``agent_mode="manage"``
调过来就能拿到知识库概况。
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage
from loguru import logger

from agent.state import AgentState
from core.db.files import get_files_by_owner
from core.kb_access import resolve_kb_owner


async def manage_agent_node(state: AgentState) -> dict:
    """
    知识库管理节点：把当前 owner 的知识库现状整理成概况返回。

    不发 LLM —— 这是**确定性操作**，用提示词去"理解意图"只会引入不确定性
    （同一句话两次可能给不同结果），而它要回答的问题本来就有唯一答案。
    """
    # 归属必须走 core/kb_access 的单一裁决点 —— 与聊天检索、/api/kb/files 同源。
    #
    # 原来这里直接用 `state["owner_id"]`，于是 admin 角色但 owner_id ≠ 共享知识库
    # 所有者的账号会看到**两个不同的知识库**：知识库页面显示 14 个文件，而
    # 问它"列出知识库文件"却答"知识库为空"。实测踩到（线上 `agent_mode=manage`
    # 返回"知识库为空"，而同一账号在页面上看得到 14 个文件）。
    # 这与之前 kb_routes 那个"读侧写侧 owner 不一致"是同一类问题的第三个面。
    owner_id = resolve_kb_owner(state.get("owner_id"))
    if owner_id is None:
        logger.info("[ManageAgent] 未登录且未开放匿名访问，不展示知识库")
        message = "🔒 当前未登录，无法查看知识库。请先登录后再试。"
        return {
            "final_answer": message,
            "reasoning_log": ["🔒 未登录，未展示知识库"],
            "messages": [AIMessage(content=message)],
        }

    logger.info("[ManageAgent] 汇报知识库概况, owner_id={}", owner_id)

    try:
        result_message, reasoning = await _handle_list(owner_id)
    except Exception as e:
        logger.error("[ManageAgent] 操作失败: {}", e)
        result_message = f"❌ 操作失败: {e!s}"
        reasoning = [f"❌ 错误: {str(e)[:200]}"]

    return {
        "final_answer": result_message,
        "reasoning_log": reasoning,
        "messages": [AIMessage(content=result_message)],
    }


async def _handle_list(owner_id: int) -> tuple[str, list]:
    """列出该 owner 的所有文件 + 向量库统计。"""
    files = await get_files_by_owner(owner_id)
    from rag.vector_store import get_collection_stats

    # Chroma 是同步阻塞调用，必须丢线程池，否则卡住事件循环
    stats = await asyncio.to_thread(get_collection_stats, owner_id)

    if not files:
        return (
            "## 知识库为空\n\n还没有上传任何文档。"
            "请在「知识库管理」页面上传个人简历、项目文档、技术笔记等资料。",
            ["📭 知识库为空"],
        )

    lines = [
        "## 知识库概况",
        "| 指标 | 数值 |",
        "|------|------|",
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
