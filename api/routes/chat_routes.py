"""
对话问答路由（SSE 流式）
"""

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from loguru import logger

from api.sse_stream import sse_chat_generator
from core.auth import get_current_user, get_optional_user
from core.database import (
    delete_conversation,
    get_chat_by_conversation_id,
    get_chat_history,
    get_chat_history_count,
    get_chat_log_by_id,
    get_conversations,
)
from core.schemas import (
    ChatHistoryResponse,
    ChatLogOut,
    ChatRequest,
    ConversationListResponse,
    ConversationSummary,
)

router = APIRouter(prefix="/api/chat", tags=["对话"])


@router.post("/stream")
async def chat_stream(
    body: ChatRequest,
    user: dict = Depends(get_current_user),
):
    """
    SSE 流式对话接口。
    返回 text/event-stream 格式的流式数据。

    事件类型:
    - reasoning: ReAct 推理步骤（整行）
    - reasoning_delta: 模型真实思考的 token 增量（推理模型）
    - answer: 回答文本（逐 Token）
    - evidence: 本轮回答的知识库证据（JSON 数组字符串，每条含
      source / score / content / chunk_idx）
    - usage: 真实 token 用量
    - done: 流式结束（含 conversation_id 与权威全文）
    - error: 错误信息
    """
    # 新对话自动生成 conversation_id，续接对话沿用已有 ID
    cid = body.conversation_id or str(uuid.uuid4())

    logger.info(
        f"[API] 流式对话: user={user['username']}, "
        f"mode={body.agent_mode}, cid={cid[:8]}..., query='{body.message[:60]}...'"
    )

    return StreamingResponse(
        sse_chat_generator(
            user_query=body.message,
            owner_id=user["owner_id"],
            username=user["username"],
            agent_mode=body.agent_mode,
            conversation_id=cid,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/stream/public")
async def chat_stream_public(
    body: ChatRequest,
    user: dict | None = Depends(get_optional_user),
):
    """
    公开流式对话接口（无需登录，但未登录用户无知识库访问权限）。
    未登录时 owner_id=0，知识库检索结果为空。
    """
    owner_id = user["owner_id"] if user else 0
    username = user["username"] if user else "guest"
    # 与需登录接口保持一致：新对话也要生成 conversation_id，
    # 否则这条记录没有分组 ID，历史列表里会散成单条
    cid = body.conversation_id or str(uuid.uuid4())

    logger.info(f"[API] 公开流式对话: user={username}, mode={body.agent_mode}")

    return StreamingResponse(
        sse_chat_generator(
            user_query=body.message,
            owner_id=owner_id,
            username=username,
            agent_mode=body.agent_mode,
            conversation_id=cid,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/history", response_model=ChatHistoryResponse)
async def get_history(
    limit: int = 50,
    offset: int = 0,
    user: dict = Depends(get_current_user),
):
    """获取当前用户的对话历史"""
    logs = await get_chat_history(user["owner_id"], limit=limit, offset=offset)
    total = await get_chat_history_count(user["owner_id"])

    return ChatHistoryResponse(
        conversations=[
            ChatLogOut(
                id=log["id"],
                owner_id=log["owner_id"],
                agent_mode=log["agent_mode"],
                conversation_id=log.get("conversation_id"),
                question=log["question"],
                answer=log["answer"],
                reasoning=log.get("reasoning"),
                sources=log.get("sources"),
                created_at=log["created_at"],
            )
            for log in logs
        ],
        total=total,
    )


@router.get("/conversation/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    user: dict = Depends(get_current_user),
):
    """
    获取指定对话的完整上下文（所有历史消息，按时间升序）。
    用于点击历史对话后恢复完整对话上下文。
    """
    owner_id = user["owner_id"]

    # 旧记录（无 conversation_id）：group_id 格式为 '__single_<id>'
    if conversation_id.startswith("__single_"):
        log_id = int(conversation_id.replace("__single_", ""))
        log = await get_chat_log_by_id(owner_id, log_id)
        if not log:
            return {"conversation_id": conversation_id, "messages": [], "owner_id": owner_id}
        return {
            "conversation_id": conversation_id,
            "owner_id": owner_id,
            "messages": [
                {"role": "user", "content": log["question"], "created_at": log["created_at"]},
                {
                    "role": "assistant",
                    "content": log["answer"],
                    "reasoning": log.get("reasoning"),
                    "sources": log.get("sources"),
                    "created_at": log["created_at"],
                },
            ],
            "total_turns": 1,
        }

    # 正常对话
    logs = await get_chat_by_conversation_id(owner_id, conversation_id)
    if not logs:
        return {"conversation_id": conversation_id, "messages": [], "owner_id": owner_id}

    messages = []
    for log in logs:
        messages.append(
            {
                "role": "user",
                "content": log["question"],
                "created_at": log["created_at"],
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": log["answer"],
                "reasoning": log.get("reasoning"),
                "sources": log.get("sources"),
                "created_at": log["created_at"],
            }
        )

    return {
        "conversation_id": conversation_id,
        "owner_id": owner_id,
        "messages": messages,
        "total_turns": len(logs),
    }


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(
    limit: int = 30,
    user: dict = Depends(get_current_user),
):
    """
    获取按对话分组的摘要列表（侧边栏用）。
    每个条目代表一个独立对话，含轮数、标题、最后活跃时间。
    """
    groups = await get_conversations(user["owner_id"], limit=limit)
    return ConversationListResponse(
        conversations=[
            ConversationSummary(
                group_id=g["group_id"],
                first_log_id=g["first_log_id"],
                turn_count=g["turn_count"],
                last_at=g.get("last_at"),
                first_question=g.get("first_question"),
            )
            for g in groups
        ],
        total=len(groups),
    )


@router.delete("/conversation/{group_id}")
async def remove_conversation(
    group_id: str,
    user: dict = Depends(get_current_user),
):
    """
    删除指定对话组（含该组内所有轮次的消息）。
    group_id 可以是 UUID（真实对话）或 __single_<id>（旧记录）。
    """
    deleted = await delete_conversation(user["owner_id"], group_id)
    if deleted == 0:
        return {"success": False, "message": "对话不存在或无权删除"}
    logger.info(
        f"[API] 删除对话: user={user['username']}, group={group_id[:20]}..., deleted={deleted} rows"
    )
    return {"success": True, "message": f"已删除 {deleted} 条消息", "deleted": deleted}
