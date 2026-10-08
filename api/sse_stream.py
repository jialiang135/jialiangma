"""
SSE 流式输出封装
================

把 LangGraph Agent 的执行过程包装成 FastAPI 的 ``text/event-stream``。

事件协议
--------
===============  ==========================================================
type             含义
===============  ==========================================================
``reasoning``    整行"步骤"（工具调用、检索结果、节点状态）。已是一整行，
                 前端无需再拼接。可带 ``icon`` 字段。
``reasoning_delta`` 模型**真实思考**的 token 增量（推理模型）。与
                 ``reasoning`` 是两类东西：前者是模型的内心独白，后者是
                 我们标记的流程步骤。分开事件类型，前端才能分别渲染。
``answer``       答案的 token 增量。
``evidence``     本轮回答的知识库证据：``content`` 为 JSON 数组，每条含
                 ``source``（文档名）/ ``score``（相关度，数值）/ ``content``
                 （片段正文，截断到 800 字）/ ``chunk_idx``（片段在源文档中的
                 块序号，取不到为 ``null``）。供前端"证据轨"渲染。
``usage``        本次调用的真实 token 用量（含 reasoning / cache 明细）。
``done``         流结束。``content`` 为 JSON，含 ``conversation_id`` 与
                 ``answer``（权威全文，前端用它替换流式过程中显示的文本）。
``error``        错误。
===============  ==========================================================

为什么 ``done`` 要带权威 answer
-------------------------------
ReAct 循环里模型会被调用多轮，中间轮次也可能吐出正文（"我先查一下……"）。
这些增量已经通过 ``answer`` 发给了前端（保证首字足够快），但它们不是最终答案。
因此结束时以 **agent 节点返回的 ``final_answer``** 为准下发给前端替换显示，
并以此落库 —— 保证"用户看到的"与"存进数据库的"是同一条文本。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

from langchain_core.messages import HumanMessage
from loguru import logger

from agent.graph_workflow import get_agent_graph
from agent.state import AgentState
from config.settings import settings
from core.db.engine import run_async_blocking
from core.llm import extract_reasoning_delta, extract_usage

# 落库时思考文本的截断上限，避免单轮对话把 reasoning 列撑爆
MAX_STORED_THINKING = 8000

# 证据轨里单条片段正文的截断上限。原文常上千字，整条 evidence 事件是
# 一个 JSON 数组，不截断会把单帧 SSE 撑到几十 KB。前端只作展示，
# 若要精确回溯某个片段，应凭 chunk_idx 另行取数（本次不做句级引用）。
MAX_EVIDENCE_CONTENT = 800

# 产出最终答案的节点（它们的输出里带 final_answer）
_FINAL_ANSWER_NODES = ("chat_agent", "manage_agent", "eval_agent")


def _initial_state(
    user_query: str,
    owner_id: int,
    username: str,
    agent_mode: str,
    conversation_id: str | None = None,
) -> AgentState:
    return {
        "messages": [HumanMessage(content=user_query)],
        "owner_id": owner_id,
        "username": username,
        "agent_mode": agent_mode,
        "conversation_id": conversation_id,
        "user_query": user_query,
        "retrieved_docs": [],
        "knowledge_context": "",
        "reasoning_log": [],
        "tool_calls": [],
        "final_answer": "",
        "needs_tool_call": False,
        "available_tools": [],
        "iteration_count": 0,
        "error": None,
    }


def _accumulate_usage(totals: dict, usage: dict) -> None:
    if not usage:
        return
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cached_tokens",
    ):
        totals[key] = totals.get(key, 0) + usage.get(key, 0)
    totals["llm_calls"] = totals.get("llm_calls", 0) + 1


def _build_evidence(docs: list) -> list[dict]:
    """
    把检索节点的 ``retrieved_docs`` 转成"证据轨"数组。

    元素结构固定为 ``{source, score, content, chunk_idx}``；顺序与
    ``docs`` 一致，前端据此与 answer 对齐。

    为什么要带 ``chunk_idx``：它是片段在源文档中的块序号，将来前端
    "点某一句 → 回溯到具体片段"需要它定位。本次**只透出**，不做句级
    引用（那要改提示词，风险高，不在本次范围）。旧数据 / 工具路径可能
    没有该字段 —— 取不到就给 ``null``，绝不报错。
    """
    evidence: list[dict] = []
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        content = doc.get("content") or ""
        if len(content) > MAX_EVIDENCE_CONTENT:
            content = content[:MAX_EVIDENCE_CONTENT] + "…"
        evidence.append(
            {
                "source": doc.get("source"),
                # score 保持数值原样透出，前端自行决定显示精度
                "score": doc.get("score"),
                "content": content,
                # chunk_idx == 0 是合法值，只能用 .get 兜底为 None，不能用 or
                "chunk_idx": doc.get("chunk_idx"),
            }
        )
    return evidence


def _persist_turn(
    owner_id: int,
    agent_mode: str,
    question: str,
    answer: str,
    steps: list[str],
    thinking: str,
    sources: list[str],
    evidence: list[dict],
    conversation_id: str | None,
    usage: dict,
):
    """落库 + 计费（协程工厂）。调用方决定是 await 还是跨线程跑。"""
    from core.db.chats import insert_chat_log
    from core.token_tracker import track_usage

    async def _run() -> None:
        # 没有有效答案就不写对话记录：否则历史里会出现一条空的助手消息。
        # 常见于用户在模型还在思考时就点了"停止"。
        if answer.strip():
            try:
                await insert_chat_log(
                    owner_id=owner_id,
                    agent_mode=agent_mode,
                    question=question,
                    answer=answer,
                    reasoning=json.dumps(
                        {
                            "steps": steps,
                            "thinking": thinking[:MAX_STORED_THINKING],
                            # 证据轨：与 SSE 的 evidence 事件同一份数组
                            # （content 已截断到 800 字）。前端恢复历史对话时
                            # 从这个字段取，不必再查向量库。
                            "evidence": evidence,
                        },
                        ensure_ascii=False,
                    ),
                    sources=json.dumps(sources, ensure_ascii=False),
                    conversation_id=conversation_id,
                )
            except Exception as e:
                logger.error("[SSE] 保存对话日志失败: {}", e)
        else:
            logger.info("[SSE] 无有效答案（可能是客户端提前中断），不写对话记录")

        # 计费无论如何都要记：token 已经真实消耗掉了
        if usage.get("total_tokens"):
            try:
                await track_usage(
                    owner_id=owner_id,
                    model=settings.deepseek_model,
                    prompt_tokens=usage.get("input_tokens", 0),
                    completion_tokens=usage.get("output_tokens", 0),
                    reasoning_tokens=usage.get("reasoning_tokens", 0),
                    cached_tokens=usage.get("cached_tokens", 0),
                )
            except Exception as e:
                logger.error("[SSE] Token 记录失败: {}", e)

    return _run()


async def sse_chat_generator(
    user_query: str,
    owner_id: int,
    username: str = "admin",
    agent_mode: str = "chat",
    conversation_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """
    真实流式生成器（``astream_events``）。

    首字延迟由"完整生成时间"降到"首个 token 到达时间"。
    """
    graph = get_agent_graph()
    state = _initial_state(user_query, owner_id, username, agent_mode, conversation_id)

    answer_parts: list[str] = []
    thinking_parts: list[str] = []
    steps: list[str] = []
    sources: list[str] = []
    evidence: list[dict] = []
    usage: dict = {}
    authoritative_answer = ""

    def persist():
        """
        生成一次落库操作。

        每次调用都要产生**新的协程对象**（协程只能被 await 一次），
        因此这里是个工厂而不是预先建好的协程。
        """
        return _persist_turn(
            owner_id=owner_id,
            agent_mode=agent_mode,
            question=user_query,
            answer=authoritative_answer or "".join(answer_parts),
            steps=steps,
            thinking="".join(thinking_parts),
            sources=sources,
            evidence=evidence,
            conversation_id=conversation_id,
            usage=usage,
        )

    try:
        async for event in graph.astream_events(state, version="v2"):
            kind = event.get("event", "")
            name = event.get("name", "")

            if kind == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk is None:
                    continue
                # 思考增量（推理模型）
                reasoning = extract_reasoning_delta(chunk)
                if reasoning:
                    thinking_parts.append(reasoning)
                    yield _sse_event("reasoning_delta", reasoning)
                # 答案增量
                content = getattr(chunk, "content", None)
                if isinstance(content, str) and content.strip():
                    answer_parts.append(content)
                    yield _sse_event("answer", content)

            elif kind == "on_chat_model_end":
                _accumulate_usage(usage, extract_usage(event.get("data", {}).get("output")))

            elif kind == "on_tool_start":
                step = f"🔍 调用工具: {name}"
                steps.append(step)
                yield _sse_event("reasoning", step, icon="🔍")

            elif kind == "on_tool_end":
                preview = str(event.get("data", {}).get("output", ""))[:200].replace("\n", " ")
                step = f"📋 {name} 返回: {preview}..."
                steps.append(step)
                yield _sse_event("reasoning", step, icon="📋")

            elif kind == "on_chain_end":
                output = event.get("data", {}).get("output")
                if not isinstance(output, dict):
                    continue
                # 只认**节点级**的 on_chain_end —— 判据是它有父 run。
                #
                # 编译后的图自己也会发一次 on_chain_end（实测 name='LangGraph'、
                # parent_ids=[]），而那次的 output 是**累积后的整个 state**：
                # 其中的 reasoning_log 是"所有节点写过的条目之和"。原实现对这个
                # 事件照单迭代，于是每一步又被重发一遍 —— 前端"查看推理过程"
                # 里条条重复、落库的历史记录同样重复（浏览器测试报告 BUG-01，
                # 回归用例见 tests/test_evidence.py::TestReasoningNoDuplicate）。
                #
                # 节点级事件的 output 才是该节点的**增量**，正是这里要的东西。
                # 用 `== []` 而不是 `not ...`：字段缺失（老版本/假事件）时按
                # 节点处理，宁可多收一条日志，也不要让推理面板整块变空。
                if event.get("parent_ids") == []:
                    continue
                # 检索节点：记录来源 + 下发证据轨，供落库与前端展示
                if name == "retrieve":
                    docs = output.get("retrieved_docs", []) or []
                    for doc in docs:
                        if isinstance(doc, dict) and doc.get("source"):
                            sources.append(doc["source"])
                    # 结构化事件 content 为 JSON 字符串（与 usage/done 一致）。
                    # 即便为空数组也照发：前端据此知道本轮无证据可渲染。
                    evidence = _build_evidence(docs)
                    yield _sse_event("evidence", json.dumps(evidence, ensure_ascii=False))
                # agent 节点：权威答案 + 节点自己记录的推理步骤
                if name in _FINAL_ANSWER_NODES and output.get("final_answer"):
                    authoritative_answer = output["final_answer"]
                for entry in output.get("reasoning_log", []) or []:
                    if isinstance(entry, str):
                        steps.append(entry)
                        yield _sse_event("reasoning", entry)

    except asyncio.CancelledError:
        # 客户端主动断开（点"停止"）。
        #
        # 这里必须**同步**落库，不能 await：任务一旦被 cancel，后续任何 await
        # 都会立刻再抛 CancelledError（且它继承自 BaseException，except Exception
        # 接不住），导致持久化根本没执行。SQLite 本地写入是毫秒级，此时请求已
        # 结束，短暂阻塞是划算的 —— 换来的是"已生成的答案不会丢"。
        logger.info("[SSE] 客户端中断，保存已生成内容")
        try:
            run_async_blocking(persist())
        except BaseException as e:
            logger.error("[SSE] 中断后保存失败: {}", e)
        raise

    except Exception as e:
        logger.error("[SSE] 流式执行异常: {}", e)
        # 尚未产出任何内容 → 降级为一次性返回（真实降级，而不是假装打字机）
        if not answer_parts and not authoritative_answer:
            try:
                final_state = await graph.ainvoke(state)
                text = final_state.get("final_answer", "") or ""
                if text:
                    answer_parts.append(text)
                    authoritative_answer = text
                    yield _sse_event("answer", text)
                for entry in final_state.get("reasoning_log", []) or []:
                    if isinstance(entry, str):
                        steps.append(entry)
                        yield _sse_event("reasoning", entry)
                # 降级路径同样补一条证据轨：检索多半已经成功，
                # 别因为流式失败就让前端拿到空证据。
                evidence = _build_evidence(final_state.get("retrieved_docs", []))
                yield _sse_event("evidence", json.dumps(evidence, ensure_ascii=False))
                yield _sse_event("reasoning", "⚠️ 流式不可用，已降级为一次性返回", icon="⚠️")
            except Exception as fallback_err:
                logger.error("[SSE] 降级执行也失败: {}", fallback_err)
                yield _sse_event("error", "服务暂时不可用，请稍后重试")
                yield _sse_event(
                    "done",
                    json.dumps(
                        {"status": "error", "conversation_id": conversation_id or ""},
                        ensure_ascii=False,
                    ),
                )
                return
        else:
            yield _sse_event("error", "生成过程中断，请重试")

    # ── 收尾：用量 → 落库 → done ──
    if usage.get("total_tokens"):
        yield _sse_event("usage", json.dumps(usage, ensure_ascii=False))

    try:
        await persist()
    except Exception as e:
        logger.error("[SSE] 保存失败: {}", e)

    yield _sse_event(
        "done",
        json.dumps(
            {
                "status": "stream_complete",
                "conversation_id": conversation_id or "",
                "answer": authoritative_answer or "".join(answer_parts),
                "usage": usage,
            },
            ensure_ascii=False,
        ),
    )


def _sse_event(event_type: str, data: str, **extra) -> str:
    """
    格式化为标准 SSE 数据行。

    Args:
        event_type: 事件类型，见模块 docstring。
        data:       文本内容。
        **extra:    附加字段（如 ``icon``），会并入 JSON 顶层。
    """
    payload = {"type": event_type, "content": data}
    if extra:
        payload.update(extra)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
