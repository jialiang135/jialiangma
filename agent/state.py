"""
LangGraph 全局 Agent 状态定义
"""

import operator
from collections.abc import Sequence
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    """LangGraph 多智能体全局状态"""

    # 对话消息历史
    messages: Annotated[Sequence[BaseMessage], operator.add]

    # 当前用户身份
    owner_id: int
    username: str

    # 当前选择的 Agent 模式: chat / manage / eval
    agent_mode: str

    # 当前对话 ID。
    # 用于把**历史轮次**喂给模型 —— 原实现不带历史，导致追问（"那它呢"）没有上下文。
    conversation_id: str | None

    # 当前用户输入的原始问题
    user_query: str

    # 检索到的知识库文档
    retrieved_docs: list[dict]

    # 知识库上下文（拼接后的字符串）
    knowledge_context: str

    # ReAct 推理过程日志（逐步展示给用户）
    # 使用 operator.add 确保各节点的日志追加而非覆盖
    reasoning_log: Annotated[list[str], operator.add]

    # 工具调用记录
    tool_calls: list[dict]

    # 最终回答
    final_answer: str

    # 是否需要进行工具调用（ReAct 循环控制）
    needs_tool_call: bool

    # 可用工具列表
    available_tools: list

    # 循环迭代次数（防止死循环）
    iteration_count: int

    # 错误信息
    error: str | None

    # --- 知识库管理专用 ---
    upload_files: list[str] | None
    operation: str | None  # upload / delete / list / rebuild
    operation_result: str | None

    # --- 预留扩展字段 ---
