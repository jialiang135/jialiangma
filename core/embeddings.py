"""
文本向量化（DashScope）
=======================

自己实现而不复用 ``langchain_community.embeddings.DashScopeEmbeddings``：

1. 全项目只用到 community 的这一个类，却要拖入整个 legacy 包
   （以及它依赖的 ``langchain-classic``）。v1 世代里 community 已无干净的
   升级线路，为 30 行代码背这个负担不划算。
2. community 的 ``BATCH_SIZE`` 表里**没有 text-embedding-v4**，会 fallback 到
   批量 25。v4 的真实批量上限更低，分块多的文档会整批失败。
3. 需要保证返回顺序与输入顺序严格一致（DashScope 用 ``text_index`` 标识，
   不排序就会把向量和文本错配）。
"""
from __future__ import annotations

from loguru import logger
from langchain_core.embeddings import Embeddings
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# 各模型单次请求的文本条数上限。控制在官方限制内，
# 宁可多切几次也不要让整批因超限而失败。
BATCH_SIZE_BY_MODEL = {
    "text-embedding-v1": 25,
    "text-embedding-v2": 25,
    "text-embedding-v3": 10,
    "text-embedding-v4": 10,
}
DEFAULT_BATCH_SIZE = 10


class EmbeddingError(RuntimeError):
    """向量化失败。"""


class DashScopeEmbeddings(Embeddings):
    """DashScope 文本向量化客户端。

    Args:
        model:         模型名，如 ``text-embedding-v4``。
        api_key:       DashScope API Key。
        batch_size:    单次请求文本条数；为 None 时按模型查表。
        max_retries:   失败重试次数（指数退避）。
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        batch_size: int | None = None,
        max_retries: int = 3,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.batch_size = batch_size or BATCH_SIZE_BY_MODEL.get(model, DEFAULT_BATCH_SIZE)
        self.max_retries = max_retries

    # ── 内部 ──

    def _call_batch(self, texts: list[str]) -> list[list[float]]:
        """
        向量化一批文本，按 text_index 还原顺序。

        外面套了熔断器：DashScope 挂掉时快速失败，而不是每个请求都去撞一遍。
        注意这里熔断层**不做重试**（``max_retries=0``）—— 重试由 tenacity 负责，
        两层各管一件事：tenacity 管"这一次调用要不要再试"，熔断器管
        "这个服务整体还能不能用"。
        """
        from core.circuit_breaker import embedding_circuit_breaker

        return embedding_circuit_breaker.call_sync(self._call_batch_once, texts)

    def _call_batch_once(self, texts: list[str]) -> list[list[float]]:
        import dashscope

        resp = dashscope.TextEmbedding.call(
            api_key=self.api_key,
            model=self.model,
            input=texts,
        )
        if resp.status_code != 200:
            raise EmbeddingError(
                f"DashScope embedding 失败: {resp.status_code} - {resp.message}"
            )

        items = (resp.output or {}).get("embeddings") or []
        if len(items) != len(texts):
            raise EmbeddingError(
                f"返回条数不符: 期望 {len(texts)}, 实际 {len(items)}"
            )
        # text_index 标识原始位置，必须排序后再取，否则向量与文本错配
        ordered = sorted(items, key=lambda it: it.get("text_index", 0))
        return [it["embedding"] for it in ordered]

    def _embed_with_retry(self, texts: list[str]) -> list[list[float]]:
        @retry(
            reraise=True,
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type(Exception),
        )
        def _do() -> list[list[float]]:
            return self._call_batch(texts)

        return _do()

    # ── Embeddings 接口 ──

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化文档分块。"""
        if not texts:
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            # 空串会让 API 报错，用单空格占位并保持位置对齐
            batch = [t if t and t.strip() else " " for t in batch]
            vectors.extend(self._embed_with_retry(batch))
            logger.debug(
                "向量化进度: {}/{} (batch={})",
                min(start + self.batch_size, len(texts)),
                len(texts),
                self.batch_size,
            )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """向量化单条查询。"""
        return self._embed_with_retry([text if text and text.strip() else " "])[0]
