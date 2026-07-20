"""
检索结果缓存
=============
基于 TTL 的检索缓存，避免相同问题重复计算 Embedding + Rerank。

使用方式：
    1. 直接调用 ``cached_retrieve()`` 作为 ``retrieve()`` 的缓存增强版本。
    2. 或在调用 ``retrieve(use_cache=True)`` 时自动启用缓存（推荐）。

缓存 key = md5(query + owner_id + 检索参数)，TTL 默认 1 小时。
"""
import hashlib
import time
from threading import Lock

from loguru import logger

# ── TTL 缓存实现 ─────────────────────────────────────────────────────────


class TTLCache:
    """
    线程安全的 TTL 缓存。

    当缓存条目数超过 ``maxsize`` 时，淘汰最旧的条目（FIFO 策略）。
    """

    def __init__(self, maxsize: int = 128, ttl: int = 3600):
        """
        Args:
            maxsize: 最大缓存条目数。
            ttl:     生存时间，单位秒（默认 1 小时）。
        """
        self._maxsize = maxsize
        self._ttl = ttl
        self._cache: dict[str, dict] = {}
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    def get(self, key: str):
        """
        获取缓存值。若 key 不存在或已过期返回 ``None``。
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                if time.time() - entry["ts"] < self._ttl:
                    self._hits += 1
                    return entry["value"]
                # 过期条目直接删除
                del self._cache[key]
            self._misses += 1
            return None

    # ------------------------------------------------------------------
    def set(self, key: str, value) -> None:
        """写入缓存条目（若达到 maxsize 则淘汰最旧条目）。"""
        with self._lock:
            if len(self._cache) >= self._maxsize:
                oldest_key = min(self._cache, key=lambda k: self._cache[k]["ts"])
                del self._cache[oldest_key]
            self._cache[key] = {"value": value, "ts": time.time()}

    # ------------------------------------------------------------------
    @property
    def stats(self) -> dict:
        """缓存统计信息。"""
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 4) if total > 0 else 0.0,
            "size": len(self._cache),
            "maxsize": self._maxsize,
            "ttl": self._ttl,
        }

    # ------------------------------------------------------------------
    def clear(self) -> None:
        """清空缓存并重置统计。"""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0
        logger.info("检索结果缓存已清空")


# 全局缓存实例（128 条，1 小时 TTL）
_cache = TTLCache(maxsize=128, ttl=3600)


# ── 工具函数 ──────────────────────────────────────────────────────────────


def _make_cache_key(
    query: str,
    owner_id: int,
    top_k_search: int = 10,
    top_k_rerank: int = 5,
    use_hybrid: bool = False,
    bm25_weight: float = 0.3,
) -> str:
    """
    生成缓存键: md5(query + owner_id + 检索参数)

    不同的参数组合会生成不同的缓存条目。
    """
    raw = f"{query}|{owner_id}|{top_k_search}|{top_k_rerank}|{use_hybrid}|{bm25_weight}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# ── 对外接口 ──────────────────────────────────────────────────────────────


def cached_retrieve(
    query: str,
    owner_id: int,
    top_k_rerank: int = 5,
    use_hybrid: bool = False,
    bm25_weight: float = 0.3,
) -> dict:
    """
    带缓存的检索入口 —— 对 ``retrieve()`` 的便捷封装。

    内部调用 ``retrieve(use_cache=True)``，由 ``retriever.retrieve`` 管理缓存读写。
    """
    from rag.retriever import retrieve

    return retrieve(
        query=query,
        owner_id=owner_id,
        top_k_search=10,
        top_k_rerank=top_k_rerank,
        use_hybrid=use_hybrid,
        bm25_weight=bm25_weight,
        use_cache=True,
    )


def get_cache_stats() -> dict:
    """获取全局缓存统计（命中数 / 未命中数 / 命中率 / 大小）。"""
    return _cache.stats


def clear_cache() -> None:
    """手动清空全局缓存。"""
    _cache.clear()
