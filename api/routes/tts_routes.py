"""
语音合成 WebSocket 路由
=======================

前端在流式对话过程中，把"已经完整的一句"通过本端点送进来合成，音频以
**二进制帧**推回，前端边收边播 —— 目标是"文字边出、声音边跟"的体感。

为什么用 WebSocket 而不是复用 SSE
--------------------------------
SSE 是文本协议，音频要走 base64（体积膨胀 33%），且与文字事件耦合在一起
不好拆分。WebSocket 支持二进制帧，天然适合持续的双向音频流。

鉴权为什么不用 ``?token=``
--------------------------
浏览器无法给 WebSocket 设置自定义请求头，常见做法是把 token 放在 query 里，
但那样 **token 会出现在 Nginx / 网关的访问日志里**（URL 会被完整记录）。
所以这里改为：**连接建立后，第一条消息必须是 auth**，超时未认证即断开。

消息协议
--------
客户端 → 服务端::

    {"type": "auth",  "token": "<JWT>"}          # 必须是第一条
    {"type": "speak", "id": 1, "text": "..."}    # 请求合成一句
    {"type": "cancel"}                           # 停止当前合成并丢弃排队
    {"type": "ping"}

服务端 → 客户端::

    {"type": "ready"}                            # 认证通过，可以开始送文本
    {"type": "started", "id": 1}                 # 该句开始合成
    <binary>                                     # 音频块（mp3）
    {"type": "done",    "id": 1}                 # 该句合成结束
    {"type": "error",   "id": 1, "message": "..."}
    {"type": "pong"}

合成是**串行**的：按收到的顺序逐句合成。因为 TTS 比 LLM 出字快
（实测合成 7 秒音频约需 3 秒），串行即可保证播放连续，同时不会打爆
DashScope 侧的并发配额。

``speak`` 的 ``text`` **可以原样送 LLM 的 Markdown 回答**，服务端会先过
:func:`core.tts.strip_markdown` 再合成 —— 这样各客户端不必各写一份剥离逻辑。
但**切句由客户端负责**：服务端按收到的顺序逐条合成，不重切。
"""

import asyncio
import contextlib
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from core.tts import strip_markdown, synthesize_stream, tts_available

router = APIRouter(prefix="/api/tts", tags=["语音"])

# 认证超时：连上后必须在该时间内发出 auth
_AUTH_TIMEOUT_SECONDS = 10
# 单句文本长度上限（防止客户端送超长文本打爆配额）
_MAX_TEXT_LENGTH = 1000


def _user_from_token(token: str | None) -> dict | None:
    """校验 JWT，返回用户信息；失败返回 None。"""
    if not token:
        return None
    try:
        from core.auth import decode_token

        payload = decode_token(token)
        if not payload:
            return None
        return {
            "owner_id": int(payload.get("sub")),
            "username": payload.get("username", ""),
        }
    except Exception as e:
        logger.debug("[TTS] token 解析失败: {}", e)
        return None


async def _send_json(ws: WebSocket, payload: dict) -> None:
    await ws.send_text(json.dumps(payload, ensure_ascii=False))


async def _synth_worker(
    ws: WebSocket,
    queue: "asyncio.Queue",
    cancel_event: asyncio.Event,
) -> None:
    """串行消费合成队列：逐句合成并把音频块推给客户端。"""
    while True:
        item = await queue.get()
        if item is None:  # 结束信号
            break

        job_id, text = item

        # 送合成前必须剥掉 Markdown：客户端送来的多半是 LLM 原文，
        # 直接合成会把 ** ## | 逐字念出来。放在服务端而不是让各客户端
        # 各自实现一份，是为了只有一处权威逻辑（见 core.tts 模块 docstring）。
        speak_text = strip_markdown(text)
        if not speak_text:
            # 整段都是代码块/图片这类不可朗读内容，跳过且不报错
            logger.debug("[TTS] 任务 {} 剥离后无内容，跳过", job_id)
            await _send_json(ws, {"type": "done", "id": job_id})
            continue

        try:
            await _send_json(ws, {"type": "started", "id": job_id})
            async for chunk in synthesize_stream(speak_text):
                if cancel_event.is_set():
                    logger.info("[TTS] 任务 {} 被取消", job_id)
                    break
                await ws.send_bytes(chunk)
            await _send_json(ws, {"type": "done", "id": job_id})
        except WebSocketDisconnect:
            break
        except Exception as e:
            logger.error("[TTS] 合成失败 id={}: {}", job_id, e)
            try:
                await _send_json(ws, {"type": "error", "id": job_id, "message": str(e)[:200]})
            except Exception:
                break
        finally:
            cancel_event.clear()


@router.websocket("/stream")
async def tts_stream(ws: WebSocket) -> None:
    """语音合成流式端点（协议见模块 docstring）。"""
    available, reason = tts_available()
    if not available:
        await ws.accept()
        await _send_json(ws, {"type": "error", "message": f"语音合成不可用: {reason}"})
        await ws.close(code=1011)
        return

    await ws.accept()

    # ── 认证：必须是第一条消息 ──
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=_AUTH_TIMEOUT_SECONDS)
        first = json.loads(raw)
    except (TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
        await ws.close(code=4401, reason="auth required")
        return

    user = _user_from_token(first.get("token")) if first.get("type") == "auth" else None
    if not user:
        await _send_json(ws, {"type": "error", "message": "未认证"})
        await ws.close(code=4401, reason="unauthorized")
        return

    logger.info("[TTS] 连接建立: user={}", user["username"])
    await _send_json(ws, {"type": "ready"})

    queue: asyncio.Queue = asyncio.Queue()
    cancel_event = asyncio.Event()
    worker = asyncio.create_task(_synth_worker(ws, queue, cancel_event))

    try:
        while True:
            message = json.loads(await ws.receive_text())
            mtype = message.get("type")

            if mtype == "speak":
                text = (message.get("text") or "").strip()
                if not text:
                    continue
                if len(text) > _MAX_TEXT_LENGTH:
                    text = text[:_MAX_TEXT_LENGTH]
                # 上一个任务已被取消时，先复位，避免新句子立刻被跳过
                cancel_event.clear()
                await queue.put((message.get("id"), text))

            elif mtype == "cancel":
                # 丢弃还没开始合成的，并中断正在合成的
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                cancel_event.set()

            elif mtype == "ping":
                await _send_json(ws, {"type": "pong"})

    except WebSocketDisconnect:
        logger.info("[TTS] 连接断开: user={}", user["username"])
    except Exception as e:
        logger.error("[TTS] 连接异常: {}", e)
    finally:
        cancel_event.set()
        await queue.put(None)
        worker.cancel()
        # 等 worker 收尾：它可能已在退出途中，取消异常是正常情况
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker
