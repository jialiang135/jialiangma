"""
SSE 流式输出封装
将 LangGraph Agent 的流式响应包装为 FastAPI SSE 格式
"""
import json
import asyncio
from typing import AsyncGenerator, Optional
from langchain_core.messages import HumanMessage
from loguru import logger

from agent.state import AgentState
from agent.graph_workflow import get_agent_graph
from config.settings import settings


async def sse_chat_generator(
    user_query: str,
    owner_id: int,
    username: str = "admin",
    agent_mode: str = "chat",
    conversation_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """
    SSE 流式生成器。
    使用 LangGraph astream_events 实现逐 Token 流式输出。

    SSE 事件类型:
    - reasoning: ReAct 推理过程（工具调用、思考步骤）
    - answer: 最终回答文本（逐 Token）
    - done: 流式结束
    - error: 错误信息

    用法（FastAPI）:
        return StreamingResponse(
            sse_chat_generator(query, owner_id, username, mode),
            media_type="text/event-stream",
        )
    """
    # 构建初始状态
    initial_state: AgentState = {
        "messages": [HumanMessage(content=user_query)],
        "owner_id": owner_id,
        "username": username,
        "agent_mode": agent_mode,
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
        "upload_files": None,
        "operation": None,
        "operation_result": None,
    }

    graph = get_agent_graph()

    # 累积变量（用于最终持久化）
    answer_chunks = []
    reasoning_entries = []

    try:
        async for event in graph.astream_events(initial_state, version="v2"):
            kind = event.get("event", "")

            if kind == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk and hasattr(chunk, "content") and chunk.content:
                    content = chunk.content
                    if hasattr(chunk, "tool_calls") and chunk.tool_calls:
                        continue
                    if isinstance(content, str) and content.strip():
                        answer_chunks.append(content)
                        yield _sse_event("answer", content)

            elif kind == "on_tool_start":
                tool_name = event.get("name", "unknown")
                msg = f"🔍 调用工具: {tool_name}"
                reasoning_entries.append(msg)
                logger.info(f"[SSE] {msg}")
                yield _sse_event("reasoning", msg)

            elif kind == "on_tool_end":
                tool_name = event.get("name", "unknown")
                output = event.get("data", {}).get("output", "")
                output_preview = str(output)[:200].replace("\n", " ")
                msg = f"📋 {tool_name} 返回: {output_preview}..."
                reasoning_entries.append(msg)
                yield _sse_event("reasoning", msg)

            elif kind == "on_chain_end":
                node_name = event.get("name", "")
                if node_name in ("chat_agent", "manage_agent", "eval_agent"):
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict):
                        for entry in output.get("reasoning_log", []):
                            if isinstance(entry, str):
                                reasoning_entries.append(entry)
                                yield _sse_event("reasoning", entry)

        # 持久化对话日志
        final_answer = "".join(answer_chunks)
        if final_answer:
            try:
                from core.database import insert_chat_log
                insert_chat_log(
                    owner_id=owner_id,
                    agent_mode=agent_mode,
                    question=user_query,
                    answer=final_answer,
                    reasoning=json.dumps(reasoning_entries, ensure_ascii=False),
                    conversation_id=conversation_id,
                )
            except Exception as e:
                logger.error(f"[SSE] 保存对话日志失败: {e}")

            # 记录 Token 使用（估算）
            try:
                from core.token_tracker import track_usage, estimate_tokens
                pt = estimate_tokens(user_query) + estimate_tokens(
                    "system prompt with knowledge context"
                )
                ct = estimate_tokens(final_answer)
                track_usage(
                    owner_id=owner_id,
                    model=settings.deepseek_model,
                    prompt_tokens=max(pt, 1),
                    completion_tokens=max(ct, 1),
                )
            except Exception as e:
                logger.error(f"[SSE] Token 记录失败: {e}")

        yield _sse_event("done", json.dumps({
            "status": "stream_complete",
            "conversation_id": conversation_id or "",
        }))

    except asyncio.CancelledError:
        logger.info("[SSE] 客户端中断了流式连接")
        yield _sse_event("error", "连接被中断")
    except Exception as e:
        logger.error(f"[SSE] 流式输出异常: {e}")
        yield _sse_event("error", f"流式输出异常: {str(e)}")
        yield _sse_event("done", "stream_error")


async def sse_simple_generator(
    user_query: str,
    owner_id: int,
    username: str = "admin",
    agent_mode: str = "chat",
    conversation_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """
    简化版 SSE 生成器。
    使用 graph.ainvoke() 整体执行，然后模拟流式输出推理日志和最终回答。
    当 astream_events 不可用时作为降级方案。
    """
    from core.database import insert_chat_log

    initial_state: AgentState = {
        "messages": [HumanMessage(content=user_query)],
        "owner_id": owner_id,
        "username": username,
        "agent_mode": agent_mode,
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
        "upload_files": None,
        "operation": None,
        "operation_result": None,
    }

    graph = get_agent_graph()

    try:
        # 先发送推理开始事件
        yield _sse_event("reasoning", f"🤔 收到问题: {user_query[:100]}...")

        # 如果非 chat 模式，注入 operation
        if agent_mode == "manage":
            initial_state["operation"] = "list"

        # 执行图
        final_state = await graph.ainvoke(initial_state)

        # 发送推理日志
        reasoning_log = final_state.get("reasoning_log", [])
        for entry in reasoning_log:
            if isinstance(entry, str):
                yield _sse_event("reasoning", entry)

        # 发送最终回答
        answer = final_state.get("final_answer", "")
        if answer:
            # 逐段流式发送（保留换行和段落结构）
            # 用 split(' ') 而非 split() 以保留 \n 换行符
            words = answer.split(' ')
            buffer = ""
            for i, word in enumerate(words):
                sep = "\n" if buffer.endswith("\n") or not buffer else " "
                buffer += sep + word
                # 每5个词、或遇到换行、或最后一段时发送
                if (i + 1) % 5 == 0 or "\n" in word or i == len(words) - 1:
                    yield _sse_event("answer", buffer)
                    buffer = ""
                    await asyncio.sleep(0.02)  # 打字机延迟

        # 保存对话日志
        try:
            insert_chat_log(
                owner_id=owner_id,
                agent_mode=agent_mode,
                question=user_query,
                answer=answer,
                reasoning=json.dumps(reasoning_log, ensure_ascii=False),
                sources=json.dumps(
                    [d.get("source", "") for d in final_state.get("retrieved_docs", [])],
                    ensure_ascii=False,
                ),
                conversation_id=conversation_id,
            )
        except Exception as e:
            logger.error(f"[SSE] 保存对话日志失败: {e}")

        # 记录 Token 使用（估算）
        try:
            from core.token_tracker import track_usage, estimate_tokens

            # 尝试从最终状态的消息中提取真实 token 数
            prompt_tokens = 0
            completion_tokens = 0
            for msg in final_state.get("messages", []):
                if hasattr(msg, "response_metadata"):
                    usage = msg.response_metadata.get("token_usage", {})
                    if usage:
                        pt = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
                        ct = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
                        prompt_tokens += pt
                        completion_tokens += ct

            # 降级：根据文本估算
            if not prompt_tokens:
                prompt_tokens = estimate_tokens(user_query) + estimate_tokens(
                    final_state.get("knowledge_context", "")
                )
            if not completion_tokens:
                completion_tokens = estimate_tokens(answer)

            prompt_tokens = max(prompt_tokens, 1)
            completion_tokens = max(completion_tokens, 1)

            track_usage(
                owner_id=owner_id,
                model=settings.deepseek_model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        except Exception as e:
            logger.error(f"[SSE] Token 记录失败: {e}")

        yield _sse_event("done", json.dumps({
            "status": "stream_complete",
            "conversation_id": conversation_id or "",
        }))

    except asyncio.CancelledError:
        yield _sse_event("error", "连接被中断")
    except Exception as e:
        logger.error(f"[SSE] 执行异常: {e}")
        yield _sse_event("error", f"执行异常: {str(e)}")
        yield _sse_event("done", "stream_error")


def _sse_event(event_type: str, data: str) -> str:
    """
    格式化为标准 SSE 数据行。
    """
    payload = json.dumps({"type": event_type, "content": data}, ensure_ascii=False)
    return f"data: {payload}\n\n"
