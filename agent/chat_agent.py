"""
问答对话 Agent —— ReAct 推理循环节点
"""

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from loguru import logger

from agent.prompts import CHAT_AGENT_SYSTEM_PROMPT
from agent.state import AgentState
from agent.tools import (
    get_chat_context,
    get_kb_summary,
    list_my_files,
    search_knowledge_base,
    verify_answer_against_kb,
)
from config.context import get_context
from core.circuit_breaker import CircuitOpenError, llm_circuit_breaker
from core.kb_access import resolve_kb_owner
from core.telemetry import span
from rag.retriever import format_context_for_prompt, retrieve

# 问答 Agent 可用工具（5个）
CHAT_TOOLS = [
    search_knowledge_base,  # 核心：语义检索
    get_kb_summary,  # 新增：知识库全貌
    list_my_files,  # 文件列表
    get_chat_context,  # 新增：对话历史
    verify_answer_against_kb,  # 新增：自我校验防幻觉
]


# 注入提示词的历史轮数上限（一轮 = 一问一答）。
# 控制上限的原因：历史全量塞进去会迅速吃满上下文预算，而推理模型的
# max_tokens 还要和思考共享，留太少给答案会被挤空。
MAX_HISTORY_TURNS = 6
# 单条历史回答的截断长度（历史只需保留要点，不必逐字回放）
MAX_HISTORY_ANSWER_CHARS = 1200

# ReAct 循环允许的最大工具调用轮数。
MAX_REACT_ITERATIONS = 5

# 轮次用尽仍未收敛时的兜底回答。
# 为什么必须显式给一条：原实现里第 5 轮的工具请求在 should_continue_chat
# 处被静默丢弃、直接走 END，而 final_answer 自始至终没被赋值（一直是 ""）。
# api/sse_stream.py 见 final_answer 为空，会退而把 5 轮产生的**过渡语**
# （"我先查一下……"）拼起来当答案下发并落库 —— 用户和数据库拿到的都不是
# 真答案。这里给一条明确的兜底，堵死"过渡语上位"这条路。
REACT_OVERFLOW_ANSWER = (
    "抱歉，我在限定的轮次内未能完成知识库检索，暂时无法给出可靠的回答。"
    "请把问题问得更具体一些（例如指明具体的项目、技能或时间段），我再试一次。"
)

# 单条工具结果注入 messages 前的长度上限。
# 依据：一次 search_knowledge_base 会拼回 top_k=5 个片段，每片段正文常上千字，
# 加上"来源/相关度"前缀，单轮轻松 6000+ 字；5 轮累积就是数万字，足以把上下文
# 预算吃光（推理模型的 max_tokens 还要和答案共享）。4000 字（中文约 2.5~3k
# tokens）能覆盖绝大多数单轮检索内容，同时约束多轮累积总量。
MAX_TOOL_RESULT_CHARS = 4000

# 截断时的标注：必须让模型知道"你看到的不是全部"，否则它会拿残缺内容
# 当完整证据下结论。
TOOL_RESULT_TRUNCATED_MARK = "…（内容已截断）"


def _truncate_tool_result(text: str) -> str:
    """把过长的工具结果截断到上限，并留下明确的截断标注。"""
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_TOOL_RESULT_CHARS] + TOOL_RESULT_TRUNCATED_MARK


async def _load_recent_history(state: AgentState) -> list:
    """
    载入当前对话的历史轮次（供模型理解追问）。

    原实现只把**当前这一句**发给模型，历史完全不传 —— 前端界面能看到历史、
    也传了 conversation_id，但模型看不到，于是"那它呢？""再详细说说"这类
    追问全部没有上下文。这里从 chat_logs 按 conversation_id 取最近若干轮补上。
    """
    from core.db.chats import get_chat_by_conversation_id

    conversation_id = state.get("conversation_id")
    if not conversation_id:
        return []

    try:
        # 默认值必须是 0（匿名），不能是 1 —— 否则匿名请求一旦带上某个
        # conversation_id，就会去读**管理员**的对话历史。真实用户的历史
        # 由 state 里的 owner_id 决定，匿名查不到任何记录。
        logs = await get_chat_by_conversation_id(state.get("owner_id", 0), conversation_id)
    except Exception as e:
        logger.warning("[ChatAgent] 读取历史对话失败: {}", e)
        return []

    history: list = []
    for log in logs[-MAX_HISTORY_TURNS:]:
        question = (log.get("question") or "").strip()
        answer = (log.get("answer") or "").strip()
        if question:
            history.append(HumanMessage(content=question))
        if answer:
            history.append(AIMessage(content=answer[:MAX_HISTORY_ANSWER_CHARS]))
    return history


async def build_chat_messages(state: AgentState) -> list:
    """
    构建对话消息列表：系统提示词 + 检索上下文 + 历史消息。
    """
    system_content = CHAT_AGENT_SYSTEM_PROMPT

    # 注入知识库检索结果（如果有）
    context = state.get("knowledge_context", "")
    if context:
        system_content += f"\n\n## 当前知识库检索结果\n以下是针对用户问题检索到的知识库内容，请基于这些内容回答：\n\n{context}"

    messages = [SystemMessage(content=system_content)]

    # 历史轮次（跨请求的对话上下文）
    history = await _load_recent_history(state)
    if history:
        messages.append(
            SystemMessage(
                content=(
                    "## 此前的对话\n以下是本次对话中更早的问答，"
                    "用于理解用户当前的追问；知识库检索结果以上文为准。"
                )
            )
        )
        messages.extend(history)

    # 本轮 ReAct 循环内的消息（工具调用与返回），保证 LLM 能看到工具结果
    react_messages = state.get("messages", [])
    for msg in react_messages:
        if isinstance(msg, (HumanMessage, AIMessage, ToolMessage)):
            messages.append(msg)

    # 当前问题只在开头注入一次。
    # 原实现用 `messages[-1].content != user_query` 判断"最新消息里有没有它"——
    # 一旦循环里追过工具调用，最后一条就变成 ToolMessage，判断恒为真，于是
    # **第 2 轮起每一轮都把同一个 user_query 再追加一遍**，越往后越重复。
    # 改为看 ReAct 循环消息里是否已包含它（该列表首条恒为当前 query）。
    user_query = state.get("user_query", "")
    if user_query and not any(
        isinstance(m, HumanMessage) and m.content == user_query for m in react_messages
    ):
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

    llm = get_context().chat.chat_model(temperature=0.3, streaming=True)
    llm_with_tools = llm.bind_tools(CHAT_TOOLS)

    messages = await build_chat_messages(state)
    iteration = state.get("iteration_count", 0)

    try:
        # 走熔断器：连续失败到阈值就快速失败，不再反复去撞已经挂掉的 API。
        # 重试与指数退避由熔断器统一负责（注意是 await asyncio.sleep，不阻塞事件循环）。
        with span("llm.invoke", iteration=iteration, message_count=len(messages)):
            response = await llm_circuit_breaker.call(llm_with_tools.ainvoke, messages)

        # 检查是否有工具调用
        if response.tool_calls and iteration < MAX_REACT_ITERATIONS:
            tool_names = [tc["name"] for tc in response.tool_calls]
            logger.info("[ChatAgent] LLM 请求工具调用: {}", tool_names)

            return {
                "messages": [response],
                "needs_tool_call": True,
                "reasoning_log": [
                    f"🔍 第{iteration + 1}轮推理 → 调用工具: {', '.join(tool_names)}"
                ],
                "iteration_count": iteration + 1,
            }

        # 达到轮次上限却仍在请求工具：这里**不能**像原来那样把请求丢掉，
        # 让 final_answer 空着（上层会拿过渡语凑答案）。给一条明确的兜底，
        # 并把"溢出"记进 reasoning_log 与日志，让运维看得见。
        if response.tool_calls:
            tool_names = [tc["name"] for tc in response.tool_calls]
            logger.warning(
                "[ChatAgent] ReAct 达到最大轮次上限({})，仍有工具请求 {}，返回兜底回答",
                MAX_REACT_ITERATIONS,
                tool_names,
            )
            return {
                "messages": [response],
                "final_answer": REACT_OVERFLOW_ANSWER,
                "needs_tool_call": False,
                "reasoning_log": [
                    f"⚠️ 已达最大工具调用轮次({MAX_REACT_ITERATIONS})，"
                    f"未能完成检索（最后一次仍请求: {', '.join(tool_names)}），返回兜底回答"
                ],
            }

        # 无工具调用 → 最终回答
        logger.info("[ChatAgent] LLM 输出最终回答 ({} 字符)", len(response.content))

        return {
            "messages": [response],
            "final_answer": response.content,
            "needs_tool_call": False,
            "reasoning_log": ["✅ 推理完成，生成最终回答"],
        }

    except CircuitOpenError as e:
        # 熔断已打开：不是本次请求的问题，而是服务整体不可用，直接给兜底回答
        logger.warning("[ChatAgent] LLM 熔断中，返回兜底回答: {}", e)
        fallback = llm_circuit_breaker.get_fallback_response(state.get("user_query", ""))
        return {
            "messages": [AIMessage(content=fallback)],
            "final_answer": fallback,
            "needs_tool_call": False,
            "error": str(e),
            "reasoning_log": ["🚨 LLM 服务熔断中，返回兜底回答"],
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

    owner_id = state.get("owner_id", 0)
    # 知识库类工具的归属由 core/kb_access.py 统一裁决（匿名默认拿不到）。
    # 原来这里写死 `KB_OWNER_ID = 1`，与检索层同一个越权洞。
    kb_owner = resolve_kb_owner(owner_id)
    tool_messages = []
    reasoning_updates = []

    for tc in tool_calls:
        tool_name = tc["name"]
        tool_args = tc.get("args", {})
        tool_call_id = tc["id"]

        # 注入 owner_id：对话历史永远用用户自己的；
        # 知识库类工具走 kb_owner —— 匿名时**直接拒绝**，
        # 而不是把管理员的知识库偷偷给他
        if tool_name == "get_chat_context":
            tool_args["owner_id"] = owner_id
        elif kb_owner is None:
            logger.info("[ToolExecutor] 拒绝匿名访问知识库工具: {}", tool_name)
            tool_messages.append(
                ToolMessage(
                    content="当前未登录，无法访问知识库。如需查询知识库内容，请先登录。",
                    tool_call_id=tool_call_id,
                )
            )
            reasoning_updates.append("🔒 未登录，已拦截知识库工具调用")
            continue
        else:
            tool_args["owner_id"] = kb_owner

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
                raw_result = str(result)
                # 工具结果原样塞回 messages 会随轮数迅速撑爆上下文，这里做长度兜底；
                # 截断时附标注，让模型知道自己看到的不是全部。
                result_str = _truncate_tool_result(raw_result)
                if len(result_str) != len(raw_result):
                    reasoning_updates.append(
                        f"✂️ {tool_name} 结果过长，已截断 {len(raw_result)} → {len(result_str)} 字符"
                    )
            else:
                result_str = f"未知工具: {tool_name}"

            reasoning_updates.append(f"🛠️ 执行 {tool_name} → 返回 {len(result_str)} 字符")

        except Exception as e:
            result_str = f"工具执行失败: {e!s}"
            reasoning_updates.append(f"❌ {tool_name} 执行失败: {str(e)[:100]}")
            logger.error(f"[ToolExecutor] {tool_name} 失败: {e}")

        tool_messages.append(ToolMessage(content=result_str, tool_call_id=tool_call_id))

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

    # ── 访问控制 ──
    # 必须用 state 里的**真实 owner_id**，不能写死。原来这里硬编码
    # `owner_id=1`，导致匿名访客（`/api/chat/stream/public` 传 0）
    # 也能检索到管理员的完整知识库 —— 实测能问出手机号与邮箱。
    # 规则见 core/kb_access.py。
    kb_owner = resolve_kb_owner(state.get("owner_id"))
    if kb_owner is None:
        logger.info("[ChatAgent] 未登录且未开放匿名访问，跳过知识库检索")
        return {
            "knowledge_context": "",
            "retrieved_docs": [],
            "reasoning_log": ["🔒 未登录，本次不使用知识库作答"],
        }

    # retrieve 内部是同步的：一次 Chroma 向量扫描 + 两次 DashScope 同步 HTTP
    # （查询向量化 + rerank）。直接在 async 节点里调用会**卡住整个事件循环**，
    # 同进程的所有请求一起排队 —— 这是全系统最主要的阻塞源，必须丢线程池。
    with span("retrieve", query_length=len(user_query), owner_id=kb_owner):
        result = await asyncio.to_thread(
            retrieve, query=user_query, owner_id=kb_owner, top_k_rerank=5
        )

    # ── 检索「故障」与「确实没有相关内容」必须分开 ──
    # 原实现里向量库异常会返回空列表，与"知识库里本来就没有"完全不可区分，
    # 于是回答"我的知识库中没有这方面的信息" —— 用户得到一个自信的错误结论。
    if result.get("error"):
        logger.error("[ChatAgent] 知识库检索失败: {}", result["error"])
        return {
            "knowledge_context": "",
            "retrieved_docs": [],
            "reasoning_log": ["⚠️ 知识库检索服务暂时不可用，本次未使用知识库作答"],
        }

    if not result["documents"]:
        logger.info("[ChatAgent] 知识库检索为空，将使用兜底回复")
        return {
            "knowledge_context": "",
            "retrieved_docs": [],
            "reasoning_log": ["📭 知识库中未找到相关内容"],
        }

    context = format_context_for_prompt(result)
    top = result["documents"][0]
    logger.info(
        "[ChatAgent] 检索到 {} 条结果, top_score={}",
        result["count"],
        top.get("score"),
    )

    # 降级（例如 rerank 服务挂了）时分数不可比，如实说明而不是假装正常
    degraded_note = "（rerank 不可用，按检索顺序排列）" if result.get("degraded") else ""
    score_text = f"{top['score']:.3f}" if isinstance(top.get("score"), (int, float)) else "未知"

    return {
        "knowledge_context": context,
        "retrieved_docs": result["documents"],
        "reasoning_log": [
            f"📚 检索到 {result['count']} 条相关知识，"
            f"最佳匹配: {top['source']} (相关度: {score_text}){degraded_note}"
        ],
    }
