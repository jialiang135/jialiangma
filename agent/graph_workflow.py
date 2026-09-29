"""
LangGraph 工作流编排
多 Agent 状态调度、节点流转、ReAct 循环终止条件
"""

from langgraph.graph import END, StateGraph
from loguru import logger

from agent.chat_agent import (
    MAX_REACT_ITERATIONS,
    chat_agent_node,
    retrieve_before_chat,
    tool_executor_node,
)
from agent.eval_agent import eval_agent_node
from agent.manage_agent import manage_agent_node
from agent.state import AgentState

# ========================================
# 路由逻辑
# ========================================


def route_to_agent(state: AgentState) -> str:
    """
    根据 agent_mode 路由到对应的 Agent 节点。

    返回的字符串必须与 add_conditional_edges 的映射表键一致，
    否则 LangGraph 会抛"无效路径"错误（原实现允许 agent_mode='eval'
    但映射表里没有 eval 分支，传进来就会崩溃）。
    """
    mode = state.get("agent_mode", "chat")
    if mode not in ("chat", "manage", "eval"):
        logger.warning(f"[Router] 未知 agent_mode={mode}，回退到 chat")
        mode = "chat"
    logger.info(f"[Router] 路由到 {mode} Agent")
    return mode


def should_continue_chat(state: AgentState) -> str:
    """
    判断是否需要继续 ReAct 循环。
    如果 LLM 请求了工具调用 → "tools"，否则 → "end"。

    **轮次上限由 chat_agent 节点自己把守**：它在 `iteration_count` 达到
    `MAX_REACT_ITERATIONS` 且模型仍要工具时，会给出兜底 `final_answer` 并把
    `needs_tool_call` 置回 False。

    原实现在这里用 `iteration < 5` 抢先把超限的那一轮判给 END，导致 chat_agent
    根本没机会产出兜底答案，`final_answer` 始终为空 —— 上层于是拿过渡语凑答案。
    所以这里只跟随 `needs_tool_call`，不再自己截断。
    """
    if not state.get("needs_tool_call", False):
        return "end"

    iteration = state.get("iteration_count", 0)
    if iteration > MAX_REACT_ITERATIONS:
        # 防御性硬上限：正常不会走到（chat_agent 到上限时已收敛并清掉 needs_tool_call）。
        logger.warning("[Graph] 超出 ReAct 硬上限({})，强制结束", MAX_REACT_ITERATIONS)
        return "end"

    logger.info(f"[Graph] ReAct 继续: iteration={iteration}")
    return "tools"


# ========================================
# 图构建
# ========================================


def create_agent_graph():
    """
    创建 LangGraph 多智能体状态图。

    图结构:
        START → router → [chat_agent | manage_agent | eval_agent]

        chat_agent 有 ReAct 子循环:
            chat_agent ⇄ tools → chat_agent → END

        manage_agent / eval_agent:
            → END
    """
    workflow = StateGraph(AgentState)

    # --- 添加节点 ---
    workflow.add_node("retrieve", retrieve_before_chat)
    workflow.add_node("chat_agent", chat_agent_node)
    workflow.add_node("tools", tool_executor_node)
    workflow.add_node("manage_agent", manage_agent_node)
    workflow.add_node("eval_agent", eval_agent_node)

    # --- 添加路由节点 ---
    workflow.add_node("router", _router_node)

    # --- 入口 → 路由 ---
    workflow.set_entry_point("router")

    # --- 路由 → Agent ---
    workflow.add_conditional_edges(
        "router",
        route_to_agent,
        {
            "chat": "retrieve",  # 先检索再问答
            "manage": "manage_agent",
            "eval": "eval_agent",
        },
    )

    # --- Chat Agent ReAct 循环 ---
    # retrieve → chat_agent
    workflow.add_edge("retrieve", "chat_agent")

    # chat_agent → tools or END
    workflow.add_conditional_edges(
        "chat_agent",
        should_continue_chat,
        {
            "tools": "tools",
            "end": END,
        },
    )

    # tools → chat_agent (回到循环)
    workflow.add_edge("tools", "chat_agent")

    # --- Manage / Eval → END ---
    workflow.add_edge("manage_agent", END)
    workflow.add_edge("eval_agent", END)

    # --- 编译 ---
    graph = workflow.compile()
    logger.info("[Graph] LangGraph 多智能体工作流编译完成")

    return graph


async def _router_node(state: AgentState) -> dict:
    """路由器节点（透传，实际的 route 逻辑在 conditional edge 中）"""
    mode = state.get("agent_mode", "chat")
    logger.info(f"[Router] 当前模式: {mode}, query='{state.get('user_query', '')[:60]}'")
    return {}


# ========================================
# 全局图实例
# ========================================

_agent_graph = None


def get_agent_graph():
    """获取全局 Agent 图实例（懒加载）"""
    global _agent_graph
    if _agent_graph is None:
        _agent_graph = create_agent_graph()
    return _agent_graph


def reset_agent_graph():
    """重置 Agent 图（用于重建知识库后刷新状态）"""
    global _agent_graph
    _agent_graph = None
    logger.info("[Graph] Agent 图已重置")
