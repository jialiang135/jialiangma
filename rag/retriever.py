"""
统一检索 + 重排链路
====================
Embedding 向量化 → ChromaDB 语义检索（owner_id 过滤）
    → [可选] BM25 + 向量混合检索 (RRF 融合)
    → [可选] 检索结果缓存 (TTL 1h)
    → DashScope Rerank 重排
    → 相关性阈值过滤

完整流程图::

    retrieve(use_hybrid=True) ──→ hybrid_search() ──┐
    retrieve(use_hybrid=False) ─→ search_by_owner() ─┤
                                                     ├──→ rerank_with_dashscope() → result
    retrieve(use_cache=True) ────→ TTLCache ────────┘

上游（Agent / API）应当这样用 ``retrieve()`` 的返回：**先看 ``error``，再看
``degraded``，最后才看 ``documents`` 空不空**：:

    result = retrieve(query, owner_id)

    if result["error"] is not None:
        # 检索链路故障（向量库/embedding 挂了）。**绝不能**告诉用户
        # "知识库里没有这方面的信息" —— 那是把故障伪装成正常结论。
        # 应回答"知识库检索服务暂时不可用"。
        ...
    elif not result["documents"]:
        # error 为 None 且没有文档 = 确实没有相关内容（含被阈值全部过滤掉）。
        # 此时可以如实说"知识库中未找到相关内容"。
        ...
    else:
        # 有结果。若 result["degraded"] 为 True，说明这次的 score 是"未评分"
        # （rerank 降级），展示时应说"分数未知/按检索顺序排列"，不要拿 score
        # 当真实相关度比较。result["filtered_count"] 是被阈值丢掉的条数。
        ...

为什么区分 ``error`` / 空文档 / ``degraded`` 很重要：这三者对应三种完全不同的
用户可见结论，混为一谈会让系统"自信地答错"。
"""

from loguru import logger

from config.settings import rerank_with_dashscope, settings
from rag.vector_store import search_by_owner


def _empty_result(query: str) -> dict:
    """
    "检索成功但没有结果"（含空查询）时的返回骨架。

    ``error`` 为 ``None`` 是关键：它表示"确实没有相关内容"，而不是链路故障。
    """
    return {
        "query": query,
        "documents": [],
        "context": "",
        "count": 0,
        "degraded": False,
        "error": None,
        "filtered_count": 0,
    }


def _format_score(score: float | None) -> str:
    """把分数格式化成展示字符串；``None``（未评分/降级）显示为"未知"。"""
    return f"{score:.4f}" if isinstance(score, (int, float)) else "未知"


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
            "documents": [
                {
                    "content": str,
                    "score": float | None,     # 正常为真实分数；降级路径为 None
                    "source": str,
                    "chunk_idx": int | None,   # 片段在源文档中的块序号（可能缺失）
                    "filepath": str | None,    # 源文件在磁盘上的路径（可能缺失）
                    "page": int | None,        # 片段所在物理页码(1-based)，仅 PDF 有值
                },
                ...
            ],
            "context": str,          # 拼接后的上下文字符串
            "count": int,            # == len(documents)
            "degraded": bool,        # 是否走了降级路径（例如 rerank 失败）
            "error": str | None,     # 检索本身失败时的原因；正常（含"确实没检索到"）为 None
            "filtered_count": int,   # 被相关性阈值过滤掉的条数
        }``

        **契约（上层请据此分支，别只判断 documents 是否为空）**：

        - ``error`` 非 ``None``：检索**链路故障**（向量库/embedding 挂了等）。
          此时 ``documents`` 必为空，但它**不代表"没有相关内容"**。上层应回
          "检索服务暂时不可用"，而**不能**说"知识库里没有"。
        - ``error`` 为 ``None`` 且 ``documents`` 为空：**检索成功但确实没有**
          相关内容（原始无结果，或被 ``retrieval_min_score`` 阈值全部过滤）。
          可如实告知"未找到相关内容"。
        - ``degraded`` 为 ``True``：走了降级路径（当前唯一来源是 rerank 失败）。
          顺序仍合理（RRF 序 / 向量距离升序），但 ``documents[i]["score"]`` 为
          ``None``（未评分），**不允许**出现伪造的 ``1.0``；展示时应说明"分数未知"。
        - ``filtered_count``：本次被阈值丢掉的条数，便于观测阈值是否过严。

    注：``chunk_idx`` / ``filepath`` / ``page`` 来自上游向量库写入的 metadata
    （见 ``core/kb_tasks.py`` 的 ``metadatas``）。之前组装时只留了 ``source``
    把它们丢掉了；现补齐，供前端"证据轨"与将来的句级回溯使用。

    ``page`` 的契约：片段所在的**物理页码**（1-based），仅 PDF 来源有值；
    其它格式 / 旧数据（未重新入库）为 ``None``。前端据此展示"出自第 N 页"，
    为 ``None`` 时不应展示页码（而非展示 0）。**注意精度**：页码来自 PDF
    文字层，且切块是**按页**进行的（chunk 不跨页）—— 见
    ``core/kb_tasks.py::_split_loaded_document`` 的说明。

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
    """实际的检索 + 重排 + 阈值过滤逻辑（无缓存）。"""
    if not query.strip():
        return _empty_result(query)

    # ── Step 1: 检索 ──
    # 检索失败必须能被感知：向量库现在会抛 VectorStoreError（见 vector_store.py），
    # 而**不是**返回空列表。这里接住它并写进 result["error"]，与"确实没检索到"
    # （error=None + 空文档）区分开。混合检索内部也会抛同一个异常，一并接住。
    search_label = "混合检索" if use_hybrid else "向量检索"
    try:
        if use_hybrid:
            from rag.bm25_search import hybrid_search

            raw_results = hybrid_search(
                query, owner_id, top_k=top_k_search, bm25_weight=bm25_weight
            )
        else:
            raw_results = search_by_owner(query, owner_id, top_k=top_k_search)
    except Exception as e:
        logger.error(
            "{} 失败: query='{}', owner_id={}, err={}",
            search_label,
            query[:50],
            owner_id,
            e,
        )
        return {**_empty_result(query), "error": f"检索失败: {e}"}

    if not raw_results:
        logger.info("{} 结果为空: query='{}', owner_id={}", search_label, query[:50], owner_id)
        # 空结果 + error=None = 检索成功但确实没有相关内容（不是故障）
        return _empty_result(query)

    logger.info("{} 到 {} 条结果, query='{}'", search_label, len(raw_results), query[:50])

    # ── Step 2: Rerank 重排 ──
    documents_text = [r["content"] for r in raw_results]
    reranked = rerank_with_dashscope(query, documents_text, top_n=top_k_rerank)

    # 降级标记：rerank 失败时每项带 degraded=True 且 score=None（不再伪造 1.0）。
    # 用 any() 而非全部判断：理论上不会只降级一半，但 any 更宽容。
    degraded = any(item.get("degraded") for item in reranked)
    min_score = settings.retrieval_min_score

    # ── Step 3: 阈值过滤 + 组装结果 ──
    documents = []
    context_parts = []
    filtered_count = 0
    for item in reranked:
        score = item.get("score")

        # 相关性阈值过滤。只在"分数可信"时生效：
        #   - 降级路径 score 为 None（未评分），无法与阈值比较 —— 此时**跳过过滤**。
        #     取舍：降级时可能把不相关片段带进 prompt；但另一条路（拿假分数过滤）
        #     会误杀，比这更糟。所以降级时不设阈值保护，由上层用 degraded 标记
        #     决定是否降权/提示。
        #   - min_score <= 0 视为关闭过滤（配置注释里已说明）。
        if not degraded and score is not None and min_score > 0 and score < min_score:
            filtered_count += 1
            continue

        idx = item["index"]
        # metadata 可能缺失（原始结果长度不足，极罕见）→ 用 .get 兜底，别让它崩
        metadata = raw_results[idx].get("metadata", {}) if idx < len(raw_results) else {}
        source = metadata.get("source", "unknown")
        # chunk_idx / filepath 是向量库写入时就带上的元数据（见 core/kb_tasks.py），
        # 这里必须显式透出、不能只留 source。为什么带 chunk_idx：它是片段在源文档
        # 中的块序号，前端"证据轨"将来要靠它把某一句回溯到**具体片段**
        # （本次只透出数据，不实现句级引用 —— 那要改提示词，风险高）。
        documents.append(
            {
                "content": item["text"],
                # 降级路径 score=None 原样保留（红线：不许伪造 1.0）
                "score": round(score, 4) if score is not None else None,
                "source": source,
                "chunk_idx": metadata.get("chunk_idx"),
                "filepath": metadata.get("filepath"),
                # page：片段所在的物理页码（1-based）。仅 PDF 有值（入库时按页
                # 切分并写入 metadata，见 core/kb_tasks.py），其它格式为 None。
                # 契约：消费侧据此展示"出自第 N 页"；为 None 时表示该来源不可回溯到页。
                "page": metadata.get("page"),
            }
        )
        context_parts.append(f"[来源: {source} | 相关度: {_format_score(score)}]\n{item['text']}")

    context = "\n\n---\n\n".join(context_parts)

    logger.info(
        "Rerank 完成: {} → {} 条 (阈值过滤 {} 条, degraded={}), top_score={}",
        len(raw_results),
        len(documents),
        filtered_count,
        degraded,
        documents[0]["score"] if documents else None,
    )

    return {
        "query": query,
        "documents": documents,
        "context": context,
        "count": len(documents),
        "degraded": degraded,
        "error": None,
        "filtered_count": filtered_count,
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
        lines.append(
            f"【片段 {i}】来源: {doc['source']} | 相关度: {_format_score(doc.get('score'))}"
        )
        lines.append(doc["content"])
        lines.append("")

    return "\n".join(lines)
