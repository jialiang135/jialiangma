"""
统一检索 + 重排链路
====================
Embedding 向量化 → ChromaDB 语义检索（owner_id 过滤）
    → [可选] BM25 + 向量混合检索 (RRF 融合)
    → [可选] 检索结果缓存 (TTL 1h)
    → DashScope Rerank 重排

完整流程图::

    retrieve(use_hybrid=True) ──→ hybrid_search() ──┐
    retrieve(use_hybrid=False) ─→ search_by_owner() ─┤
                                                     ├──→ rerank_with_dashscope() → result
    retrieve(use_cache=True) ────→ TTLCache ────────┘
"""

from loguru import logger

from config.settings import rerank_with_dashscope, settings
from rag.vector_store import search_by_owner


def retrieve(
    query: str,
    owner_id: int,
    top_k_search: int = 10,
    top_k_rerank: int = 5,
    use_hybrid: bool | None = None,
    bm25_weight: float = 0.3,
    use_cache: bool | None = None,
) -> dict:
    """
    完整的 RAG 检索链路。

    Args:
        query:         用户查询。
        owner_id:      用户 ID（数据隔离过滤）。
        top_k_search:  检索阶段候选数（传给向量库或混合检索）。
        top_k_rerank:  重排后保留的结果数。
        use_hybrid:    是否使用 BM25 + 向量混合检索。``None`` 时取
                       ``settings.use_hybrid_search``（默认开启）。
        bm25_weight:   混合检索时 BM25 的权重 (0~1)，仅 use_hybrid=True 时有效。
        use_cache:     是否启用结果缓存。``None`` 时取
                       ``settings.use_search_cache``（默认开启）。

    Returns:
        ``{
            "query": str,
            "documents": [{"content": str, "score": float, "source": str}, ...],
            "context": str,   # 拼接后的上下文字符串
            "count": int,
        }``

    注：混合检索与缓存原先默认关闭，且全仓库没有任何调用点传过参数 ——
    等于两套实现（BM25+RRF 融合、TTL 缓存）从未生效过。现在默认开启，
    缓存失效挂在向量库的写操作上（见 rag/vector_store.py）。
    """
    # 默认值取配置：None 表示"没指定，用配置"
    if use_hybrid is None:
        use_hybrid = settings.use_hybrid_search
    if use_cache is None:
        use_cache = settings.use_search_cache
    # ── 缓存处理（在最外层，避免重复计算） ──
    if use_cache:
        from rag.search_cache import _cache, _make_cache_key

        cache_key = _make_cache_key(
            query, owner_id, top_k_search, top_k_rerank, use_hybrid, bm25_weight
        )
        cached = _cache.get(cache_key)
        if cached is not None:
            logger.debug("检索缓存命中: key={}", cache_key[:16])
            return cached

    # ── 执行检索 ──
    result = _retrieve_impl(query, owner_id, top_k_search, top_k_rerank, use_hybrid, bm25_weight)

    # ── 写回缓存 ──
    if use_cache and result["documents"]:
        from rag.search_cache import _cache

        _cache.set(cache_key, result)
        logger.debug("检索结果已缓存: key={}, docs={}", cache_key[:16], result["count"])

    return result


# ====================================================================
# 内部实现（不含缓存逻辑，便于测试）
# ====================================================================


def _retrieve_impl(
    query: str,
    owner_id: int,
    top_k_search: int,
    top_k_rerank: int,
    use_hybrid: bool,
    bm25_weight: float,
) -> dict:
    """实际的检索 + 重排逻辑（无缓存）。"""
    if not query.strip():
        return {"query": query, "documents": [], "context": "", "count": 0}

    # ── Step 1: 检索 ──
    if use_hybrid:
        from rag.bm25_search import hybrid_search

        raw_results = hybrid_search(query, owner_id, top_k=top_k_search, bm25_weight=bm25_weight)
        search_label = "混合检索"
    else:
        raw_results = search_by_owner(query, owner_id, top_k=top_k_search)
        search_label = "向量检索"

    if not raw_results:
        logger.info("{} 结果为空: query='{}', owner_id={}", search_label, query[:50], owner_id)
        return {"query": query, "documents": [], "context": "", "count": 0}

    logger.info("{} 到 {} 条结果, query='{}'", search_label, len(raw_results), query[:50])

    # ── Step 2: Rerank 重排 ──
    documents_text = [r["content"] for r in raw_results]
    reranked = rerank_with_dashscope(query, documents_text, top_n=top_k_rerank)

    # ── Step 3: 组装结果 ──
    documents = []
    context_parts = []
    for item in reranked:
        idx = item["index"]
        # 如果原始结果长度不足（极罕见），跳过
        metadata = raw_results[idx].get("metadata", {}) if idx < len(raw_results) else {}
        source = metadata.get("source", "unknown")

        documents.append(
            {
                "content": item["text"],
                "score": round(item["score"], 4),
                "source": source,
            }
        )
        context_parts.append(f"[来源: {source} | 相关度: {item['score']:.4f}]\n{item['text']}")

    context = "\n\n---\n\n".join(context_parts)

    logger.info(
        "Rerank 完成: {} → {} 条, top_score={}",
        len(raw_results),
        len(documents),
        documents[0]["score"] if documents else 0,
    )

    return {
        "query": query,
        "documents": documents,
        "context": context,
        "count": len(documents),
    }


# ====================================================================
# 工具函数
# ====================================================================


def format_context_for_prompt(retrieval_result: dict) -> str:
    """
    将检索结果格式化为适合注入 LLM 提示词的上下文字符串。

    Args:
        retrieval_result: ``retrieve()`` 返回的字典。

    Returns:
        格式化的 Markdown 字符串，可直接拼入 System Prompt。
    """
    if not retrieval_result.get("documents"):
        return ""

    lines = ["以下是从知识库中检索到的相关信息：", ""]
    for i, doc in enumerate(retrieval_result["documents"], start=1):
        lines.append(f"【片段 {i}】来源: {doc['source']} | 相关度: {doc['score']}")
        lines.append(doc["content"])
        lines.append("")

    return "\n".join(lines)
