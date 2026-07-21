"""
对话记录 & 评测报告导出路由
提供 Markdown / JSON 格式的文件下载
"""
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from loguru import logger

from core.auth import get_current_user
from core.database import (
    get_chat_history,
    get_chat_by_conversation_id,
)

router = APIRouter(prefix="/api/export", tags=["导出"])


# ========================================
# Markdown 渲染工具
# ========================================

def _build_markdown_export(chats: list[dict]) -> str:
    """将聊天记录列表渲染为 Markdown 格式。

    chats 按时间升序排列，每条显示为：
        ## Q{序号}: {问题}
        **时间**: ... **模式**: ...
        ### 回答
        {回答}
        <details>...推理过程...</details>
        ---
    """
    lines: list[str] = []
    lines.append("# 个人数字分身 · 对话记录")
    lines.append(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for idx, chat in enumerate(chats, start=1):
        question = chat.get("question", "")
        answer = chat.get("answer", "")
        reasoning = chat.get("reasoning") or ""
        created_at = chat.get("created_at", "")
        agent_mode = chat.get("agent_mode", "chat")

        ts = str(created_at) if created_at else "未知"

        lines.append(f"## Q{idx}: {question}")
        lines.append(f"**时间**: {ts}  |  **模式**: {agent_mode}")
        lines.append("")
        lines.append("### 回答")
        lines.append(answer)
        lines.append("")

        if reasoning.strip():
            lines.append("<details>")
            lines.append("<summary>🧠 推理过程</summary>")
            lines.append("")
            for step_line in reasoning.split("\n"):
                stripped = step_line.strip()
                if stripped:
                    lines.append(f"- {stripped}")
            lines.append("")
            lines.append("</details>")

        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ========================================
# 导出对话 — Markdown
# ========================================

@router.get("/chat/markdown")
async def export_chat_markdown(
    conversation_id: str = Query(
        "all",
        description="对话ID；'all' 表示导出最近的所有对话",
    ),
    limit: int = Query(50, ge=1, le=500, description="conversation_id=all 时最多导出的记录条数"),
    user: dict = Depends(get_current_user),
):
    """导出对话记录为 Markdown 文件。

    - conversation_id=all（默认）→ 导出最近 limit 条记录
    - conversation_id=X → 导出该对话全部记录
    """
    owner_id = user["owner_id"]

    if conversation_id == "all":
        # 获取最近 limit 条记录，按时间升序排列（对话连贯）
        raw = get_chat_history(owner_id, limit=limit, offset=0)
        chats = list(reversed(raw))  # DESC → ASC
        filename = f"chat_export_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        logger.info(
            f"[Export] 用户 {user['username']} 导出全部对话 Markdown: {len(chats)} 条"
        )
    else:
        chats = get_chat_by_conversation_id(owner_id, conversation_id)
        if not chats:
            raise HTTPException(status_code=404, detail="未找到该对话记录")
        filename = f"chat_export_{conversation_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        logger.info(
            f"[Export] 用户 {user['username']} 导出对话 {conversation_id}: {len(chats)} 条"
        )

    md_content = _build_markdown_export(chats)

    return Response(
        content=md_content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ========================================
# 导出对话 — JSON
# ========================================

@router.get("/chat/json")
async def export_chat_json(
    limit: int = Query(50, ge=1, le=1000, description="最多导出的记录条数"),
    user: dict = Depends(get_current_user),
):
    """导出对话记录为 JSON 文件（供程序化使用）"""
    owner_id = user["owner_id"]
    raw = get_chat_history(owner_id, limit=limit, offset=0)
    chats = list(reversed(raw))  # DESC → ASC 方便阅读

    export_data = {
        "export_time": datetime.now().isoformat(),
        "total": len(chats),
        "conversations": [
            {
                "id": c["id"],
                "question": c["question"],
                "answer": c["answer"],
                "reasoning": c.get("reasoning"),
                "sources": c.get("sources"),
                "agent_mode": c.get("agent_mode"),
                "conversation_id": c.get("conversation_id"),
                "created_at": str(c["created_at"]) if c.get("created_at") else None,
            }
            for c in chats
        ],
    }

    json_content = json.dumps(export_data, ensure_ascii=False, indent=2)
    filename = f"chat_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    logger.info(
        f"[Export] 用户 {user['username']} 导出 JSON: {len(chats)} 条"
    )

    return Response(
        content=json_content,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

