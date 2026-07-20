"""
问答对话 Agent —— ReAct 推理循环节点
"""
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from loguru import logger

from agent.state import AgentState
from agent.prompts import CHAT_AGENT_SYSTEM_PROMPT
from agent.tools import (
    search_knowledge_base,
    list_my_files,
    get_kb_summary,
    get_chat_context,
    verify_answer_against_kb,
)
from config.settings import get_deepseek_llm
from rag.retriever import retrieve, format_context_for_prompt


# 问答 Agent 可用工具（5个）
CHAT_TOOLS = [
    search_knowledge_base,   # 核心：语义检索
    get_kb_summary,           # 新增：知识库全貌
    list_my_files,            # 文件列表
    get_chat_context,         # 新增：对话历史
    verify_answer_against_kb, # 新增：自我校验防幻觉
]


def build_chat_messages(state: AgentState) -> list:
    """
    构建对话消息列表：系统提示词 + 检索上下文 + 历史消息
    """
    system_content = CHAT_AGENT_SYSTEM_PROMPT

    # 注入知识库检索结果（如果有）
    context = state.get("knowledge_context", "")
    if context:
        system_content += f"\n\n## 当前知识库检索结果\n以下是针对用户问题检索到的知识库内容，请基于这些内容回答：\n\n{context}"

    messages = [SystemMessage(content=system_content)]

    # 添加历史消息（保留对话历史 + 工具调用结果，确保 ReAct 循环中 LLM 能看到工具返回）
    for msg in state.get("messages", []):
        if isinstance(msg, (HumanMessage, AIMessage, ToolMessage)):
            messages.append(msg)

    # 如果最新的消息不在历史中，添加当前查询
    user_query = state.get("user_query", "")
    if user_query and (not messages or messages[-1].content != user_query):
        messages.append(HumanMessage(content=user_query))

    return messages


async def chat_agent_node(state: AgentState) -> dict:
    """
    ReAct 问答 Agent 节点。
    每次调用时：LLM 分析当前状态 → 决定调用工具 or 输出最终答案。

    返回 AgentState 的部分更新。
    """
    logger.info(
        "[ChatAgent] 节点执行: query='{}', iteration={}",
        state.get("user_query", "")[:80],
        state.get("iteration_count", 0),
    )

    llm = get_deepseek_llm(temperature=0.3, streaming=True)
    llm_with_tools = llm.bind_tools(CHAT_TOOLS)

    messages = build_chat_messages(state)
    iteration = state.get("iteration_count", 0)

    try:
        response = await llm_with_tools.ainvoke(messages)

        # 检查是否有工具调用
        if response.tool_calls and iteration < 5:
            tool_names = [tc["name"] for tc in response.tool_calls]
            logger.info("[ChatAgent] LLM 请求工具调用: {}", tool_names)

            return {
                "messages": [response],
                "needs_tool_call": True,
                "reasoning_log": [
                    f"🔍 第{iteration+1}轮推理 → 调用工具: {', '.join(tool_names)}"
                ],
                "iteration_count": iteration + 1,
            }

        # 无工具调用 → 最终回答
        logger.info("[ChatAgent] LLM 输出最终回答 ({} 字符)", len(response.content))

        return {
            "messages": [response],
            "final_answer": response.content,
            "needs_tool_call": False,
            "reasoning_log": ["✅ 推理完成，生成最终回答"],
        }

    except Exception as e:
        logger.error(f"[ChatAgent] LLM 调用失败: {e}")
        fallback = "抱歉，AI 服务暂时不可用，请稍后重试。"
        return {
            "messages": [AIMessage(content=fallback)],
            "final_answer": fallback,
            "needs_tool_call": False,
            "error": str(e),
            "reasoning_log": [f"❌ 错误: {str(e)[:200]}"],
        }


async def tool_executor_node(state: AgentState) -> dict:
    """
    工具执行节点：执行 LLM 请求的工具调用，将结果以 ToolMessage 返回。
    """
    last_message = state["messages"][-1]
    tool_calls = last_message.tool_calls

    if not tool_calls:
        return {"needs_tool_call": False}

    owner_id = state.get("owner_id", 1)
    # KB 工具统一用管理员知识库（owner_id=1），对话历史用用户自己的
    KB_OWNER_ID = 1
    tool_messages = []
    reasoning_updates = []

    for tc in tool_calls:
        tool_name = tc["name"]
        tool_args = tc.get("args", {})
        tool_call_id = tc["id"]

        # 注入 owner_id：对话历史用用户自己的，其余工具用管理员的
        if tool_name == "get_chat_context":
            tool_args["owner_id"] = owner_id
        else:
            tool_args["owner_id"] = KB_OWNER_ID

        logger.info("[ToolExecutor] 执行: {} args={}", tool_name, tool_args)

        try:
            # 查找并执行工具
            tool_func = None
            for t in CHAT_TOOLS:
                if t.name == tool_name:
                    tool_func = t
                    break

            if tool_func:
                result = await tool_func.ainvoke(tool_args)
                result_str = str(result)
            else:
                result_str = f"未知工具: {tool_name}"

            reasoning_updates.append(
                f"🛠️ 执行 {tool_name} → 返回 {len(result_str)} 字符"
            )

        except Exception as e:
            result_str = f"工具执行失败: {str(e)}"
            reasoning_updates.append(f"❌ {tool_name} 执行失败: {str(e)[:100]}")
            logger.error(f"[ToolExecutor] {tool_name} 失败: {e}")

        tool_messages.append(
            ToolMessage(content=result_str, tool_call_id=tool_call_id)
        )

    return {
        "messages": tool_messages,
        "needs_tool_call": False,
        "reasoning_log": reasoning_updates,
    }


async def retrieve_before_chat(state: AgentState) -> dict:
    """
    在进入 chat_agent 之前，先检索知识库。
    如果检索结果为空，设置兜底标记。
    """
    user_query = state.get("user_query", "")

    if not user_query.strip():
        return {"knowledge_context": "", "retrieved_docs": []}

    # 所有用户共享同一份知识库（管理员的知识库），检索时固定查 owner_id=1
    result = retrieve(query=user_query, owner_id=1, top_k_rerank=5)

    if not result["documents"]:
        logger.info("[ChatAgent] 知识库检索为空，将使用兜底回复")
        return {
            "knowledge_context": "",
            "retrieved_docs": [],
            "reasoning_log": ["📭 知识库中未找到相关内容"],
        }

    context = format_context_for_prompt(result)
    logger.info(
        "[ChatAgent] 检索到 {} 条结果, top_score={}",
        result["count"],
        result["documents"][0]["score"] if result["documents"] else 0,
    )

    return {
        "knowledge_context": context,
        "retrieved_docs": result["documents"],
        "reasoning_log": [
            f"📚 检索到 {result['count']} 条相关知识，"
            f"最佳匹配: {result['documents'][0]['source']} "
            f"(相关度: {result['documents'][0]['score']:.3f})"
        ],
    }
