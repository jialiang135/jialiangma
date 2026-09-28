"""
LLM 流式响应工具
================

从流式 chunk 中提取**思考增量**（推理模型的 ``reasoning_content``）。

历史背景（值得记下来，因为踩过）
--------------------------------
在 langchain 0.3.x 世代（`langchain-openai` 0.2.x）里，推理模型的思考文本
在流式路径上会被**静默丢弃**：`langchain_openai` 的 delta 转换器不认识
`reasoning_content` 字段，而 `langchain_deepseek` 里那句把它写进
`additional_kwargs` 的代码被放在一个永远不成立的判断分支内（它重写的是
一个**模块级函数**而非方法，属于死代码）。

当时的表现是：`astream` 的每个 chunk 的 `additional_kwargs` 都是空的，
只有 `usage_metadata.output_token_details.reasoning` 能看出思考确实发生过。

**根治办法是升级依赖**：`langchain-openai` 1.x 原生支持该字段。
因此这里不再需要任何补丁子类，只保留一个兼容提取函数。
"""
from __future__ import annotations

from typing import Any


def extract_reasoning_delta(chunk: Any) -> str:
    """
    从流式 chunk 中取出思考增量；没有则返回空串。

    兼容两种承载方式：

    - ``additional_kwargs["reasoning_content"]`` —— langchain-openai/deepseek
      当前使用的约定；
    - ``content_blocks`` 中 ``type == "reasoning"`` 的块 —— langchain-core 1.x
      正在推的统一表示，将来 provider 可能改走这里。两条都读，避免升级即失效。
    """
    if chunk is None:
        return ""

    ak = getattr(chunk, "additional_kwargs", None) or {}
    text = ak.get("reasoning_content")
    if text:
        return text if isinstance(text, str) else str(text)

    blocks = getattr(chunk, "content_blocks", None) or []
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "reasoning":
            block_text = block.get("reasoning") or block.get("text")
            if block_text:
                parts.append(str(block_text))
    return "".join(parts)


def extract_usage(chunk_or_message: Any) -> dict:
    """
    从 chunk / message 中提取真实 token 用量。

    Returns:
        ``{"input_tokens": int, "output_tokens": int, "total_tokens": int,
           "reasoning_tokens": int, "cached_tokens": int}``
        取不到时返回空 dict（调用方据此跳过）。
    """
    um = getattr(chunk_or_message, "usage_metadata", None)
    if not um:
        return {}

    out_details = um.get("output_token_details") or {}
    in_details = um.get("input_token_details") or {}
    return {
        "input_tokens": um.get("input_tokens", 0) or 0,
        "output_tokens": um.get("output_tokens", 0) or 0,
        "total_tokens": um.get("total_tokens", 0) or 0,
        "reasoning_tokens": out_details.get("reasoning", 0) or 0,
        "cached_tokens": in_details.get("cache_read", 0) or 0,
    }
