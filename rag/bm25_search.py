"""
BM25 关键词检索 + 向量混合检索（RRF 融合排序）
=================================================
使用 rank_bm25 实现关键词检索，并与 ChromaDB 向量检索通过
Reciprocal Rank Fusion (RRF) 融合排序。

Features:
    - bm25_search(): 纯 BM25 关键词检索
    - hybrid_search(): BM25 + 向量语义的 RRF 融合检索
    - invalidate_bm25_cache(): 文档更新时使索引失效
"""

import re

from loguru import logger
from rank_bm25 import BM25Okapi

from rag.vector_store import COLLECTION_NAME, _get_chroma_client, search_by_owner

# ── 中文分词（jieba 为可选依赖） ───────────────────────────────────────────
#
# 为什么用 jieba
# --------------
# 原来的实现是"逐字 unigram"：把「人工智能」拆成 人/工/智/能 四个单字。
# 中文的词义单位是**词**不是**字**，逐字切开后词序与词边界全丢了，
# 于是 BM25 的匹配退化成"单字重叠度" —— 像「人工」与「工人」这种字相同、
# 词不同的串会被判成高度相关，短 query 时噪声尤其大。
# jieba 是中文分词事实标准，按词切分（cut 出「人工智能」整词）能显著
# 提升中文关键词检索的精确度，这也是本次改动的核心收益。
#
# 为什么仍然是**可选依赖**、并且保留降级
# ------------------------------------
# jieba 会连同词典一起进 Docker 镜像；把它做成**软依赖**，即使某天安装
# 失败（镜像体积、离线构建、依赖冲突），检索链路也只会"质量略降"而不是
# 直接报错。降级方案用 **unigram + bigram**（单字 + 相邻两字组合），
# 相对纯 unigram 多保留了词序信息（bigram「人工」「工智」「智能」能把
# 「人工智能」这种词和它被打散的邻近字区分开），且**零外部依赖**。
#
# 两种方案的代价
#   - jieba：更准（按词匹配），代价是首次 import + 建词典约 0.5~2 秒、
#     镜像多一个包；
#   - unigram+bigram：零依赖、零启动开销，但词典是"猜"的 —— 相邻两字未必
#     是词，召回与精度都不如真分词（是"有信息比没有强"的兜底）。
#
# 开销只付一次，不会摊到每次查询
# ------------------------------
# jieba **首次分词**时才加载词典（内部用 initialized 标志守卫），这一开销
# 每个进程只发生一次。BM25 索引本身按 owner_id **惰性缓存**（见
# ``BM25IndexManager.get_index``），全语料分词只发生在（重）建索引时；
# 每次查询仅需对 query 这一小段调用一次 ``_tokenize``。因此 jieba 的词典
# 加载成本 = 每进程一次，**不会**变成每次查询都付。
try:
    import jieba as _jieba

    _JIEBA_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行环境是否装了 jieba
    _jieba = None  # type: ignore[assignment]
    _JIEBA_AVAILABLE = False
    logger.warning(
        "jieba 未安装，BM25 中文分词降级为 unigram+bigram（保留词序，但不如分词精确）。"
        "安装可提升中文检索质量: pip install jieba"
    )


#: 连续中文串（一段一段地喂给分词器，避免标点/英文打断分词上下文）。
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
#: 英文单词 / 数字（与中文串互斥）。
_ALNUM_RE = re.compile(r"[a-zA-Z0-9]+")


def _unigram_bigram(cjk_run: str) -> list[str]:
    """
    无外部依赖的中文降级分词：unigram（单字）+ bigram（相邻两字组合）。

    纯 unigram 丢词序；加上 bigram 后，「人工智能」会同时产出
    ``人/工/智/能`` 与 ``人工/工智/智能`` —— 后者携带了相邻字的组合信息，
    能把「人工智能」与「智能人」这类同字异序串区分开。
    """
    chars = list(cjk_run)
    tokens = list(chars)
    tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return tokens


def _segment_chinese(cjk_run: str) -> list[str]:
    """切分一段连续中文：jieba 可用则按词切分，否则降级为 unigram+bigram。"""
    if _JIEBA_AVAILABLE and _jieba is not None:
        # jieba.lcut 对纯中文串返回词列表（不会夹带空白），直接过滤空串兜底
        return [tok for tok in _jieba.lcut(cjk_run) if tok.strip()]
    return _unigram_bigram(cjk_run)


def _tokenize(text: str) -> list[str]:
    """
    混合中英文的 BM25 分词器。

    策略（见文件顶部「中文分词」注释的完整取舍说明）：
    - **中文**：优先 ``jieba.lcut`` 按词切分（「人工智能」→ 整词）；jieba 不可用时
      降级为 unigram+bigram（「人工智能」→ 人/工/智/能 + 人工/工智/智能）。
      两者都比原来的"逐字 unigram"保留更多词序/词边界信息。
    - **英文 / 数字**：按 ``[a-zA-Z0-9]+`` 提取，统一转小写（大小写不敏感）。

    Args:
        text: 待分词的原始文本（查询或文档正文）。

    Returns:
        分词结果列表（中文词 + 小写英文/数字 token）。
    """
    if not text:
        return []

    tokens: list[str] = []
    # 中文：逐段交给分词器（中英混排时按中文串切段，避免被标点/英文切断）
    for run in _CJK_RUN_RE.findall(text):
        tokens.extend(_segment_chinese(run))
    # 英文单词 / 数字
    tokens.extend(tok.lower() for tok in _ALNUM_RE.findall(text))
    return tokens


# ── BM25 索引管理器 ──────────────────────────────────────────────────────


class BM25IndexManager:
    """
    按 owner_id 缓存 BM25 索引的管理器。

    当用户文档更新（新增 / 删除）后，调用 ``invalidate()`` 使缓存失效，
    下次检索时自动重建。
    """

    def __init__(self):
        self._indices: dict[int, tuple[BM25Okapi, list[dict]]] = {}

    # ------------------------------------------------------------------
    def build_index(self, owner_id: int) -> tuple[BM25Okapi | None, list[dict]]:
        """
        从 ChromaDB 拉取指定用户的全部文档，构建 BM25Okapi 索引。

        Returns:
            (bm25, documents)  —— BM25 模型与文档列表。
            若该用户没有文档则返回 (None, [])。
        """
        try:
            client = _get_chroma_client()
            collection = client.get_collection(COLLECTION_NAME)
        except Exception as exc:
            logger.error("获取 ChromaDB collection 失败: {}", exc)
            return None, []

        try:
            existing = collection.get(
                where={"owner_id": owner_id},
                include=["documents", "metadatas"],
            )
        except Exception as exc:
            logger.error("从 ChromaDB 拉取文档失败 (owner_id={}): {}", owner_id, exc)
            return None, []

        if not existing["ids"]:
            logger.warning("用户 {} 没有文档，BM25 索引为空", owner_id)
            return None, []

        documents: list[dict] = []
        for i in range(len(existing["ids"])):
            documents.append(
                {
                    "doc_id": existing["ids"][i],
                    "content": existing["documents"][i] if existing["documents"] else "",
                    "metadata": existing["metadatas"][i] if existing["metadatas"] else {},
                }
            )

        tokenized_corpus = [_tokenize(d["content"]) for d in documents]
        bm25 = BM25Okapi(tokenized_corpus)

        logger.info("BM25 索引构建完成: owner_id={}, docs={}", owner_id, len(documents))
        return bm25, documents

    # ------------------------------------------------------------------
    def get_index(self, owner_id: int) -> tuple[BM25Okapi | None, list[dict]]:
        """获取 (或惰性构建) 指定用户的 BM25 索引。"""
        if owner_id not in self._indices:
            result = self.build_index(owner_id)
            if result[0] is not None:
                self._indices[owner_id] = result  # type: ignore[assignment]
            else:
                return None, []
        return self._indices.get(owner_id, (None, []))

    # ------------------------------------------------------------------
    def invalidate(self, owner_id: int | None = None) -> None:
        """
        使 BM25 索引缓存失效。

        Args:
            owner_id: 指定用户；为 ``None`` 时清空所有缓存。
        """
        if owner_id is None:
            self._indices.clear()
            logger.info("BM25 索引缓存已全部清除")
        elif owner_id in self._indices:
            del self._indices[owner_id]
            logger.info("BM25 索引缓存已清除: owner_id={}", owner_id)


# 全局单例
_bm25_manager = BM25IndexManager()


# ── 对外检索函数 ──────────────────────────────────────────────────────────


def bm25_search(
    query: str,
    owner_id: int,
    top_k: int = 5,
) -> list[dict]:
    """
    BM25 关键词检索。

    Args:
        query:  查询文本。
        owner_id: 用户 ID（数据隔离）。
        top_k:   返回 Top-K 条结果。

    Returns:
        ``[{"doc_id": str, "content": str, "metadata": dict, "score": float}, ...]``
    """
    if not query.strip():
        return []

    bm25, documents = _bm25_manager.get_index(owner_id)
    if bm25 is None or not documents:
        logger.debug("BM25 索引不可用: owner_id={}", owner_id)
        return []

    tokenized_q = _tokenize(query)
    if not tokenized_q:
        return []

    scores = bm25.get_scores(tokenized_q)

    scored: list[dict] = []
    for i, s in enumerate(scores):
        if s > 0:
            scored.append(
                {
                    "doc_id": documents[i]["doc_id"],
                    "content": documents[i]["content"],
                    "metadata": documents[i]["metadata"],
                    "score": float(s),
                }
            )

    scored.sort(key=lambda x: x["score"], reverse=True)
    result = scored[:top_k]

    logger.debug(
        "BM25 检索: query='{}', owner_id={}, results={}", query[:50], owner_id, len(result)
    )
    return result


def hybrid_search(
    query: str,
    owner_id: int,
    top_k: int = 5,
    bm25_weight: float = 0.3,
) -> list[dict]:
    """
    混合检索：BM25 + 向量语义检索，使用 **加权 Reciprocal Rank Fusion (RRF)** 融合。

    RRF 公式::
        score(d) = (1 - α) * Σ [1 / (k + rank_vec(d))] + α * Σ [1 / (k + rank_bm25(d))]

    其中 α = bm25_weight, k = 60。

    Args:
        query:       查询文本。
        owner_id:    用户 ID。
        top_k:       返回 Top-K 条结果。
        bm25_weight: BM25 权重 (0~1)；向量权重 = 1 - bm25_weight。

    Returns:
        ``[{"content": str, "metadata": dict, "score": float, "source": str}, ...]``
        格式与 :func:`rag.vector_store.search_by_owner` 兼容，可直接接入重排链路。
    """
    if not query.strip():
        return []

    # 拉取更多候选给 RRF 融合
    n_candidates = top_k * 3
    vec_results = search_by_owner(query, owner_id, top_k=n_candidates)
    bm25_results = bm25_search(query, owner_id, top_k=n_candidates)

    if not vec_results and not bm25_results:
        return []

    K = 60  # RRF 常数

    # key = content 前 200 字符 → 融合后的文档
    fused: dict[str, dict] = {}

    # ── 向量贡献 ──
    for rank, doc in enumerate(vec_results, start=1):
        ckey = doc["content"][:200]
        fused[ckey] = {
            "content": doc["content"],
            "metadata": doc["metadata"].copy(),
            "score": (1 - bm25_weight) / (K + rank),
        }

    # ── BM25 贡献 ──
    for rank, doc in enumerate(bm25_results, start=1):
        ckey = doc["content"][:200]
        contrib = bm25_weight / (K + rank)

        if ckey in fused:
            fused[ckey]["score"] += contrib
            # 补全 metadata（BM25 可能带有向量结果没有的字段）
            src = doc["metadata"].get("source")
            if src and not fused[ckey]["metadata"].get("source"):
                fused[ckey]["metadata"]["source"] = src
        else:
            fused[ckey] = {
                "content": doc["content"],
                "metadata": doc["metadata"].copy(),
                "score": contrib,
            }

    # ── 排序 & 截断 ──
    sorted_results = sorted(fused.values(), key=lambda x: x["score"], reverse=True)[:top_k]

    logger.info(
        "混合检索完成: query='{}', owner_id={}, vec={}, bm25={}, fused={}",
        query[:50],
        owner_id,
        len(vec_results),
        len(bm25_results),
        len(sorted_results),
    )

    return sorted_results


def invalidate_bm25_cache(owner_id: int | None = None) -> None:
    """使 BM25 缓存失效（文档增删后调用）。"""
    _bm25_manager.invalidate(owner_id)
