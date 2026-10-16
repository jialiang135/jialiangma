"""TTL cache expiry, eviction, statistics, and retrieval delegation."""

import rag.search_cache as cache_module


def test_ttl_expiry_hit_miss_stats_and_clear(monkeypatch):
    cache = cache_module.TTLCache(maxsize=3, ttl=10)
    now = [100.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: now[0])

    assert cache.stats["hit_rate"] == 0.0
    assert cache.get("absent") is None
    cache.set("key", {"answer": 1})
    now[0] = 109.9
    assert cache.get("key") == {"answer": 1}
    now[0] = 110.0
    assert cache.get("key") is None
    assert cache.stats == {
        "hits": 1,
        "misses": 2,
        "hit_rate": 0.3333,
        "size": 0,
        "maxsize": 3,
        "ttl": 10,
    }

    cache.clear()
    assert cache.stats["hits"] == cache.stats["misses"] == cache.stats["size"] == 0


def test_ttl_cache_evicts_oldest_entry_when_capacity_is_reached(monkeypatch):
    cache = cache_module.TTLCache(maxsize=2, ttl=100)
    now = [1.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: now[0])
    cache.set("old", 1)
    now[0] += 1
    cache.set("newer", 2)
    now[0] += 1
    cache.set("latest", 3)

    assert cache.stats["size"] == 2
    assert cache.get("old") is None
    assert cache.get("newer") == 2
    assert cache.get("latest") == 3


def test_replacing_existing_key_does_not_evict_an_unrelated_entry(monkeypatch):
    cache = cache_module.TTLCache(maxsize=2, ttl=100)
    now = [1.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: now[0])
    cache.set("a", 1)
    now[0] += 1
    cache.set("b", 2)
    now[0] += 1
    cache.set("b", 22)

    assert cache.get("a") == 1
    assert cache.get("b") == 22
    assert cache.stats["size"] == 2


def test_cache_key_includes_each_retrieval_parameter():
    base = cache_module._make_cache_key("query", 1)
    assert cache_module._make_cache_key("query", 2) != base
    assert cache_module._make_cache_key("query", 1, top_k_search=11) != base
    assert cache_module._make_cache_key("query", 1, top_k_rerank=6) != base
    assert cache_module._make_cache_key("query", 1, use_hybrid=True) != base
    assert cache_module._make_cache_key("query", 1, bm25_weight=0.7) != base


def test_cached_retrieve_forwards_supported_options(monkeypatch):
    import rag.retriever as retriever

    calls = []
    monkeypatch.setattr(retriever, "retrieve", lambda **kwargs: calls.append(kwargs) or {"count": 2})
    result = cache_module.cached_retrieve(
        "question", 8, top_k_rerank=9, use_hybrid=True, bm25_weight=0.6
    )
    assert result == {"count": 2}
    assert calls == [
        {
            "query": "question",
            "owner_id": 8,
            "top_k_search": 10,
            "top_k_rerank": 9,
            "use_hybrid": True,
            "bm25_weight": 0.6,
            "use_cache": True,
        }
    ]


def test_global_cache_stats_and_clear(monkeypatch):
    cache = cache_module.TTLCache(maxsize=2, ttl=20)
    monkeypatch.setattr(cache_module, "_cache", cache)
    cache.set("one", 1)
    assert cache_module.get_cache_stats()["size"] == 1
    cache_module.clear_cache()
    assert cache_module.get_cache_stats()["size"] == 0
    assert cache_module.get_cache_stats()["hits"] == 0
