"""
两个外部模型服务的**具体实现**
================================

从 `config/settings.py` 搬出来的（原文件把"配置 / 模块级副作用 / 模型客户端工厂"
三件事混在一起，423 行）。这里只做一件事：把 `config/ports.py` 定义的接缝
实现出来。

搬移过程**逐字保留**了原来那些注释 —— 它们记录的失败经验（推理预算、连接泄漏、
降级的分数语义）比代码本身值钱。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from config.settings import Settings

if TYPE_CHECKING:
    from langchain_core.embeddings import Embeddings
    from langchain_core.language_models import BaseChatModel


class DeepSeekChatProvider:
    """DeepSeek 对话模型（`ChatModelProvider` 的实现）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # LLM 客户端缓存。
        # 必须缓存：ChatDeepSeek 内部持有一个 httpx 异步客户端，而原实现每次调用都
        # 新建一个 —— 每个对话请求都会新建客户端且从不关闭，既是连接/内存泄漏，
        # 退出时还会抛出 "Event loop is closed" 的未处理异常（close 发生在循环关闭后）。
        self._cache: dict[tuple, Any] = {}

    def chat_model(
        self,
        *,
        temperature: float = 0.3,
        streaming: bool = True,
        max_tokens: int | None = None,
    ) -> BaseChatModel:
        """
        按 (temperature, streaming, max_tokens) 缓存，同一进程内复用。

        注意 ``max_tokens`` 对推理模型是"思考 + 答案"的共享预算，详见
        ``Settings.llm_max_tokens`` 的说明。传 ``None`` 用配置的默认值；
        评测裁判会显式传一个更大的值（理由见 ``config/ports.py``）。
        """
        budget = max_tokens if max_tokens is not None else self._settings.llm_max_tokens
        key = (temperature, streaming, budget)
        cached = self._cache.get(key)
        if cached is None:
            from langchain_deepseek import ChatDeepSeek

            cached = ChatDeepSeek(
                model=self._settings.deepseek_model,
                api_key=self._settings.deepseek_api_key,
                api_base=self._settings.deepseek_base_url,
                temperature=temperature,
                streaming=streaming,
                max_tokens=budget,
            )
            self._cache[key] = cached
        return cached

    async def aclose(self) -> None:
        """
        关闭缓存的客户端（应用退出时调用）。

        不显式关闭的话，httpx 客户端会在事件循环关闭后才被回收，
        从而抛出 "Task exception was never retrieved: Event loop is closed"。
        """
        for llm in list(self._cache.values()):
            for attr in ("root_async_client", "async_client", "root_client", "client"):
                client = getattr(llm, attr, None)
                if client is None or not hasattr(client, "close"):
                    continue
                try:
                    result = client.close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception as e:
                    logger.debug("关闭 LLM 客户端 {} 失败: {}", attr, e)
        self._cache.clear()


class DashScopeProvider:
    """阿里云 DashScope：Embedding + Rerank（`EmbeddingProvider` 的实现）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def embeddings(self) -> Embeddings:
        """
        Embedding 客户端。

        实现放在 ``core/embeddings.py``（自研，不再依赖 langchain-community）：
        该包在全项目只被用到这一个类，却要拖入整个 legacy 包及其 langchain-classic
        依赖。
        """
        from core.embeddings import DashScopeEmbeddings

        return DashScopeEmbeddings(
            model=self._settings.embedding_model,
            api_key=self._settings.dashscope_api_key,
        )

    def rerank(self, query: str, documents: list[str], top_n: int = 5) -> list[dict[str, Any]]:
        """
        对检索结果重排。

        Returns:
            ``[{"index": int, "score": float | None, "text": str, "degraded": bool}, ...]``

            - 正常：``score`` 是真实的 ``relevance_score``（0~1），``degraded`` 为 False。
            - 降级（API 返回异常 / 调用抛错）：保留输入顺序，但 ``score`` 为 ``None``
              （明确表示"未评分"），``degraded`` 为 True。
        """
        import dashscope

        if not documents:
            return []

        try:
            result = dashscope.TextReRank.call(
                api_key=self._settings.dashscope_api_key,
                model=self._settings.rerank_model,
                query=query,
                documents=documents,
                top_n=min(top_n, len(documents)),
                return_documents=True,
            )
            if result.status_code == 200 and result.output:
                return [
                    {
                        "index": item["index"],
                        "score": item["relevance_score"],
                        "text": item["document"]
                        if isinstance(item["document"], str)
                        else item["document"]["text"],
                        "degraded": False,
                    }
                    for item in result.output["results"]
                ]
            logger.warning("Rerank API 返回异常: {} - {}", result.status_code, result.message)
            return _degraded_rerank(documents, top_n)
        except Exception as e:
            logger.error("Rerank API 调用失败: {}", e)
            return _degraded_rerank(documents, top_n)


def _degraded_rerank(documents: list[str], top_n: int) -> list[dict[str, Any]]:
    """
    rerank 不可用时的降级结果。

    保留输入顺序（该顺序来自"混合检索的 RRF 序"或"纯向量的距离升序"，
    两者都是合理的排序 —— **问题从来不在顺序，而在分数语义**），
    但 ``score`` 一律为 ``None``，明确表示"这次没有真正评分"。

    为什么不能再填 ``1.0``（原实现的做法）：那个假分数会被拼进上下文、
    显示成"相关度: 1.0000"，既误导 LLM 与用户，又让相关性阈值过滤彻底失效
    （假 1.0 永远高于阈值，什么都拦不住）。``None`` 让上层能如实说"分数未知"，
    并让阈值过滤在降级时**跳过**而不是被假分数骗过。
    """
    return [
        {"index": i, "score": None, "text": doc, "degraded": True}
        for i, doc in enumerate(documents[:top_n])
    ]
