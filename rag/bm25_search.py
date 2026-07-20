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
from typing import Optional

from loguru import logger
from rank_bm25 import BM25Okapi

from rag.vector_store import COLLECTION_NAME, _get_chroma_client, search_by_owner

# ── 分词 ──────────────────────────────────────────────────────────────────


def _tokenize(text: str) -> list[str]:
    """
    混合中英文的简单分词器，适用于 BM25。

    策略：
    - 中文字符逐字切分（单字 unigram），对中文关键词匹配效果足够
    - 英文单词 / 数字按空白和标点提取
    - 统一转小写，保证大小写不敏感
    """
    if not text:
        return []

    # 中文汉字单字
    chinese_chars = re.findall(r"[一-鿿]", text)
    # 英文单词 + 数字
    alpha_tokens = re.findall(r"[a-zA-Z0-9]+", text)

    return [t.lower() for t in chinese_chars + alpha_tokens]


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
    def build_index(self, owner_id: int) -> tuple[Optional[BM25Okapi], list[dict]]:
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
    def get_index(self, owner_id: int) -> tuple[Optional[BM25Okapi], list[dict]]:
        """获取 (或惰性构建) 指定用户的 BM25 索引。"""
        if owner_id not in self._indices:
            result = self.build_index(owner_id)
            if result[0] is not None:
                self._indices[owner_id] = result  # type: ignore[assignment]
            else:
                return None, []
        return self._indices.get(owner_id, (None, []))

    # ------------------------------------------------------------------
    def invalidate(self, owner_id: Optional[int] = None) -> None:
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

    logger.debug("BM25 检索: query='{}', owner_id={}, results={}", query[:50], owner_id, len(result))
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


def invalidate_bm25_cache(owner_id: Optional[int] = None) -> None:
    """使 BM25 缓存失效（文档增删后调用）。"""
    _bm25_manager.invalidate(owner_id)
