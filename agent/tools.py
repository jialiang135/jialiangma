"""
共享工具集：LangChain @tool 封装
供 LangGraph Agent 在 ReAct 循环中调用

为什么这些工具是 async 的
-------------------------
它们要访问数据库（数据层已是 async）与检索链路。检索链路（Chroma 查询 +
DashScope 的同步 HTTP）本身没有 async 版本，用 ``asyncio.to_thread`` 把它
挪出事件循环 —— 否则一次检索就会卡住整个进程的其它请求。
"""

import asyncio

from langchain_core.tools import tool
from loguru import logger

from core.database import get_chat_history, get_files_by_owner
from rag.retriever import format_context_for_prompt, retrieve
from rag.vector_store import get_collection_stats

# ========================================
# 知识库检索工具（核心）
# ========================================


@tool
async def search_knowledge_base(
    query: str,
    owner_id: int,
    top_k: int = 5,
) -> str:
    """
    在个人私有知识库中语义检索与 query 最相关的文档片段。
    这是最核心的工具——当用户问任何关于"我"的个人信息、技能、项目、经历时，必须先调用此工具检索知识库。

    适用场景:
    - 用户问"你做过什么项目" → 检索"项目经验"
    - 用户问"你熟悉哪些技术" → 检索"技术栈 技能"
    - 用户问"你之前在哪个公司" → 检索"工作经历 公司"
    - 任何需要从知识库获取信息的问题

    参数:
        query: 搜索查询文本（自然语言，建议提取关键词）
        owner_id: 当前用户ID（系统自动注入，无需手动传）
        top_k: 返回最相关的片段数量，默认5

    返回:
        格式化的检索结果字符串，包含来源文件和内容片段。
        如果知识库为空或没有相关内容，返回提示信息。
    """
    logger.info(f"[Tool] search_knowledge_base: query='{query[:80]}...', owner_id={owner_id}")

    # retrieve 内部是同步的（Chroma 查询 + DashScope 同步 HTTP），丢线程池执行
    result = await asyncio.to_thread(retrieve, query=query, owner_id=owner_id, top_k_rerank=top_k)

    if not result["documents"]:
        return "【检索结果】知识库中未找到相关内容。请如实告知用户，不要编造信息。"

    context = format_context_for_prompt(result)
    summary = (
        f"【检索结果】共找到 {result['count']} 个相关片段。"
        f"最相关片段来自: {result['documents'][0]['source']}"
        f"（相关度: {result['documents'][0]['score']}）\n\n"
    )
    return summary + context


# ========================================
# 文件查询工具
# ========================================


@tool
async def list_my_files(owner_id: int) -> str:
    """
    查询当前用户已上传的所有文件列表及知识库概况。
    当用户问"知识库有哪些文件"或"你有哪些资料"时使用。

    参数:
        owner_id: 当前用户ID（系统自动注入）

    返回:
        文件中及向量库统计信息。
    """
    logger.info(f"[Tool] list_my_files: owner_id={owner_id}")

    files = await get_files_by_owner(owner_id)
    stats = await asyncio.to_thread(get_collection_stats, owner_id)

    if not files:
        return "知识库为空，还没有上传任何文件。建议用户先上传个人简历和项目文档。"

    lines = [
        "## 知识库概况",
        f"- 📁 文件数: {len(files)}",
        f"- 🧩 向量块总数: {stats['total_chunks']}",
        "",
        "## 已上传文件列表",
    ]
    for f in files:
        size_kb = f.get("file_size", 0) / 1024
        lines.append(
            f"- **{f['filename']}** | "
            f"大小: {size_kb:.1f}KB | "
            f"分块: {f.get('chunk_count', 0)} 块 | "
            f"上传: {str(f.get('created_at', ''))[:10]}"
        )
    return "\n".join(lines)


# ========================================
# 知识库摘要工具（新增）
# ========================================


@tool
async def get_kb_summary(owner_id: int) -> str:
    """
    获取知识库的整体内容摘要——对所有文档做一个全貌概览。
    当用户问"你的知识库大概包含什么内容"或"介绍一下你自己"等宽泛问题时，
    先用此工具获取知识库全貌，再根据摘要中的线索进一步用 search_knowledge_base 深挖细节。

    参数:
        owner_id: 当前用户ID（系统自动注入）

    返回:
        知识库内容摘要，按文件列出关键信息点。
    """
    logger.info(f"[Tool] get_kb_summary: owner_id={owner_id}")

    files = await get_files_by_owner(owner_id)
    stats = await asyncio.to_thread(get_collection_stats, owner_id)

    if not files:
        return "知识库为空，无法生成摘要。请先上传文档。"

    # 对每个文件做一次概括性检索
    summaries = []
    for f in files:
        filename = f["filename"]
        # 用文件名作为查询词，检索该文件最具代表性的片段
        result = await asyncio.to_thread(
            retrieve,
            query=f"摘要 概述 主要内容 {filename.replace('.pdf', '').replace('.docx', '').replace('.txt', '')}",
            owner_id=owner_id,
            top_k_rerank=3,
        )
        if result["documents"]:
            # 提取前两个片段的关键句
            snippets = []
            for doc in result["documents"][:2]:
                text = doc["content"][:300]
                snippets.append(f"  - {text.strip()}")
            summaries.append(f"### {filename}\n" + "\n".join(snippets))
        else:
            summaries.append(f"### {filename}\n  （内容较少或无文本内容）")

    header = f"## 知识库全貌\n📁 {len(files)} 个文件 | 🧩 {stats['total_chunks']} 个向量块\n\n"
    return header + "\n\n".join(summaries)


# ========================================
# 对话历史工具（新增）
# ========================================


@tool
async def get_chat_context(owner_id: int, limit: int = 10) -> str:
    """
    获取最近的对话历史记录。当用户问"我们刚才聊了什么"或需要结合对话上下文回答时使用。

    参数:
        owner_id: 当前用户ID（系统自动注入）
        limit: 返回最近的N条对话记录，默认10

    返回:
        最近的对话历史（问题+回答摘要）。
    """
    logger.info(f"[Tool] get_chat_context: owner_id={owner_id}")

    logs = await get_chat_history(owner_id, limit=limit, offset=0)

    if not logs:
        return "没有历史对话记录。"

    lines = [f"## 最近 {len(logs)} 条对话记录", ""]
    for log in reversed(logs):  # 倒序显示（最早的在前）
        q = log.get("question", "")[:200]
        a = log.get("answer", "")[:300]
        lines.append(f"**Q:** {q}")
        lines.append(f"**A:** {a}...")
        lines.append("")
    return "\n".join(lines)


# ========================================
# 答案校验工具（评测用）（新增到对话工具集）
# ========================================


@tool
async def verify_answer_against_kb(
    claim: str,
    owner_id: int,
) -> str:
    """
    校验一条陈述/回答是否能在知识库中找到支撑依据。
    当你不确定某条信息是否准确时，用此工具做自我校验，避免幻觉。

    参数:
        claim: 需要校验的陈述文本
        owner_id: 当前用户ID（系统自动注入）

    返回:
        校验结果，包含是否有支撑、匹配程度。
    """
    logger.info(f"[Tool] verify_answer_against_kb: claim='{claim[:100]}...'")

    result = await asyncio.to_thread(retrieve, query=claim, owner_id=owner_id, top_k_rerank=3)

    if not result["documents"]:
        return (
            "【校验结果】知识库中未找到支撑该陈述的内容。\n"
            "判定: 该陈述可能为幻觉（无中生有），建议不要在回答中使用此信息。"
        )

    top_doc = result["documents"][0]
    if top_doc["score"] >= 0.5:
        return (
            f"【校验结果】✅ 知识库中找到高相关度匹配（得分: {top_doc['score']}）\n"
            f"来源: {top_doc['source']}\n"
            f"匹配内容: {top_doc['content'][:500]}\n"
            "判定: 该陈述有知识库支撑，可以使用。"
        )
    else:
        return (
            f"【校验结果】⚠️ 知识库中找到部分相关但匹配度较低的内容（得分: {top_doc['score']}）\n"
            f"来源: {top_doc['source']}\n"
            f"最相关内容: {top_doc['content'][:500]}\n"
            "判定: 该陈述可能部分偏离知识库，建议谨慎使用。"
        )


# ========================================
# 登记到工具注册中心
# ========================================
#
# 原先 tool_registry 从未被任何代码调用 —— /api/tools 恒返回空列表，
# 而这个注册中心存在的意义就是让工具可被列举（前端可视化编排面板要读它）。
# 这里做一次性登记：从 LangChain 工具对象上取 name/description/参数 schema，
# 交给注册中心，接口就有真实数据了。
def _register_all_tools() -> None:
    from core.tool_registry import tool_registry

    # (工具对象, 类别)
    specs = [
        (search_knowledge_base, "知识库检索"),
        (get_kb_summary, "知识库检索"),
        (list_my_files, "知识库管理"),
        (get_chat_context, "对话上下文"),
        (verify_answer_against_kb, "防幻觉校验"),
    ]

    for tool_obj, category in specs:
        parameters: dict = {}
        try:
            schema = getattr(tool_obj, "args_schema", None)
            if schema is not None and hasattr(schema, "model_json_schema"):
                parameters = schema.model_json_schema()
        except Exception as e:
            logger.debug("工具 {} 参数 schema 提取失败: {}", tool_obj.name, e)

        # 注册表记录的是可调用实现：async 工具用 coroutine，同步工具用 func
        impl = getattr(tool_obj, "coroutine", None) or getattr(tool_obj, "func", None)
        if impl is None:
            logger.warning("工具 {} 没有可调用的实现，跳过登记", tool_obj.name)
            continue

        tool_registry.register(
            name=tool_obj.name,
            description=tool_obj.description,
            category=category,
            parameters=parameters,
        )(impl)


_register_all_tools()
