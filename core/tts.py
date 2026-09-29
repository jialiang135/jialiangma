"""
语音合成（TTS）
===============

基于阿里云 DashScope 的 **CosyVoice** 做流式语音合成，目标体感是"文字边出、
声音边跟"（类似豆包的朗读）。

实测数据（2026-09）
------------------
- 模型：``cosyvoice-v2`` 可用（**`cosyvoice-v3.5-flash` / `cosyvoice-v3-flash`
  在本账号上返回 `Engine return error code: 418`，不可用**）
- 音色：``longxiaochun_v2`` / ``longwan_v2`` 可用
- **流式首包延迟约 0.79 秒**（53 字中文）；非流式整段约 1.1 秒
- 输出格式：mp3（128kbps）

两个容易踩的点
--------------
1. **Markdown 会被念出来**：LLM 的回答里有 ``**加粗**``、``## 标题``、表格的 ``|``，
   直接送 TTS 会念成"星号星号…"。必须先剥离 —— 见 :func:`strip_markdown`。
2. **DashScope 的合成 SDK 是同步阻塞 + 回调在子线程**：
   要桥接到 async WebSocket，必须在独立线程里跑合成，
   用 ``loop.call_soon_threadsafe`` 把音频块推进 asyncio 队列。
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import AsyncIterator

from loguru import logger

from config.settings import settings

# ============================================================
# 文本预处理
# ============================================================

# 代码块 ``` ... ``` —— 朗读代码没有意义，直接丢弃
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
# 行内代码 `x` —— 保留内容（技术名词要读出来），去掉反引号
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
# 图片 ![alt](url) —— 完全丢弃（URL 念出来是噪音）
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# 链接 [text](url) —— 只保留文字
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# 表格分隔行 |---|---|
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$", re.MULTILINE)
# 标题行首的 # 号
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
# 列表/引用行首的标记
_BULLET_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d+\.)\s+", re.MULTILINE)
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
# 强调符号 ** __ * _
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_)")
# 水平线
_HR_RE = re.compile(r"^\s*([-*_]\s*){3,}$", re.MULTILINE)
# 表格里的竖线（分隔单元格用逗号代替，避免连读）
_TABLE_PIPE_RE = re.compile(r"\s*\|\s*")


def strip_markdown(text: str) -> str:
    """
    把 Markdown 文本转成"适合朗读"的纯文本。

    处理原则：**能读的内容保留，纯排版符号丢弃**。
    - 代码块整块丢弃（朗读代码无意义，还容易被念成乱码）
    - 行内代码保留内容（`LangGraph` 这类技术名词要读出来）
    - 链接只保留可见文字、图片整条丢弃
    - 强调符号、标题号、列表号、引用符、表格竖线全部去掉
    """
    if not text:
        return ""

    out = _FENCED_CODE_RE.sub("", text)  # 先丢整块代码
    out = _IMAGE_RE.sub("", out)
    out = _LINK_RE.sub(r"\1", out)
    out = _INLINE_CODE_RE.sub(r"\1", out)
    out = _HR_RE.sub("", out)
    out = _TABLE_SEP_RE.sub("", out)
    out = _HEADING_RE.sub("", out)
    out = _BULLET_RE.sub("", out)
    out = _QUOTE_RE.sub("", out)
    # 表格竖线用顿号分隔（比逗号更像"列举"，读出来更自然）
    out = _TABLE_PIPE_RE.sub("、", out)
    out = _EMPHASIS_RE.sub("", out)

    # 收敛空白；并清掉表格转换产生的重复/首尾标点（如 "、、" 或行尾的 "、"）
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{2,}", "\n", out)
    out = re.sub(r"([、，,])\s*(?=[、，,\n])", "", out)  # 连续标点只留最后一个
    out = re.sub(r"[、，,]\s*$", "", out, flags=re.MULTILINE)
    return out.strip()


# 句子边界：中文句末标点 + 换行；英文句末标点后必须跟空白，避免切在 "3.5" 上
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])\s*|\n+")


def split_sentences(text: str, min_length: int = 4) -> list[str]:
    """
    把文本切成适合逐句合成的句子。

    Args:
        text:       待切分文本（应先过 :func:`strip_markdown`）。
        min_length: 低于该长度的片段会与后一句合并，避免"嗯。""对。"这种碎片
                    单独发起一次合成请求（每次请求都有网络开销）。

    Returns:
        句子列表（不含空串）。
    """
    if not text:
        return []

    raw = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s and s.strip()]

    merged: list[str] = []
    for piece in raw:
        if merged and len(merged[-1]) < min_length:
            merged[-1] = f"{merged[-1]}{piece}"
        else:
            merged.append(piece)
    return [s for s in merged if s]


# ============================================================
# 流式合成
# ============================================================


class TTSError(RuntimeError):
    """语音合成失败。"""


def _build_synthesizer(on_data, on_done, on_error):
    """构造 DashScope 流式合成器（在合成线程内调用）。"""
    from dashscope.audio.tts_v2 import ResultCallback, SpeechSynthesizer

    class _Callback(ResultCallback):
        def on_open(self) -> None:
            pass

        def on_complete(self) -> None:
            on_done()

        def on_error(self, message) -> None:
            on_error(message)

        def on_close(self) -> None:
            pass

        def on_event(self, result) -> None:
            pass

        def on_data(self, data) -> None:
            if data:
                on_data(data)

    return SpeechSynthesizer(
        model=settings.tts_model,
        voice=settings.tts_voice,
        callback=_Callback(),
        # 兜底：即使上游漏了 Markdown，也让引擎尽量按纯文本处理
        additional_params={"enable_markdown_filter": True},
    )


async def synthesize_stream(text: str) -> AsyncIterator[bytes]:
    """
    流式合成一段文本，逐块产出音频字节。

    **桥接方式**：DashScope 的合成是同步阻塞、回调在子线程；
    这里把合成放进独立线程，用 ``loop.call_soon_threadsafe`` 把音频块
    推进 asyncio 队列，主协程从队列里取并 yield。
    这样调用方（WebSocket 端点）可以边收边推给前端。

    Raises:
        TTSError: 合成失败（含引擎返回错误）。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    _SENTINEL = object()

    def push(item) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, item)

    def on_data(data: bytes) -> None:
        push(data)

    def on_done() -> None:
        push(_SENTINEL)

    def on_error(message) -> None:
        push(TTSError(str(message)[:300]))

    def worker() -> None:
        try:
            syn = _build_synthesizer(on_data, on_done, on_error)
            syn.streaming_call(text)
            syn.streaming_complete()
        except Exception as e:
            logger.error("[TTS] 合成线程异常: {}", e)
            push(TTSError(str(e)[:300]))

    thread = threading.Thread(target=worker, daemon=True, name="tts-synth")
    thread.start()

    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            if isinstance(item, TTSError):
                raise item
            yield item
    finally:
        # 调用方提前 break（用户点了停止）时，别让线程悬着
        if thread.is_alive():
            thread.join(timeout=0.5)


def synthesize_once(text: str, timeout: float = 60.0) -> bytes:
    """
    非流式合成：一次性拿到完整音频。

    供不需要流式的场景使用（例如服务端自检、离线生成）。
    内部仍是流式合成，只是把块拼起来。
    """
    chunks: list[bytes] = []

    async def _collect() -> None:
        async for chunk in synthesize_stream(text):
            chunks.append(chunk)

    async def _run() -> None:
        await asyncio.wait_for(_collect(), timeout=timeout)

    asyncio.run(_run())
    return b"".join(chunks)


def tts_available() -> tuple[bool, str]:
    """
    自检：TTS 是否配置可用。

    Returns:
        ``(是否可用, 说明)``
    """
    if not settings.tts_enabled:
        return False, "TTS 未启用（TTS_ENABLED=false）"
    if not settings.dashscope_api_key or settings.dashscope_api_key.startswith("sk-your"):
        return False, "DashScope API Key 未配置"
    return True, f"{settings.tts_model} / {settings.tts_voice}"
