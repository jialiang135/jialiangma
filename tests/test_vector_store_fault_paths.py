"""Offline vector-store CRUD, lazy initialization, and partial-write failures."""

import hashlib
from types import SimpleNamespace

import pytest


def test_add_documents_batches_hashes_ids_and_invalidates_each_owner(monkeypatch):
    import rag.vector_store as store

    calls = []

    class FakeStore:
        def add_texts(self, *, texts, metadatas, ids):
            calls.append((texts, metadatas, ids))
            return ids

    invalidated = []
    monkeypatch.setattr(store, "get_vector_store", lambda: FakeStore())
    monkeypatch.setattr(store, "_invalidate_retrieval_caches", invalidated.append)
    chunks = ["first", "second", "third"]
    metadata = [{"owner_id": 1}, {"owner_id": 2}, {"owner_id": 1}]

    result = store.add_documents(chunks, metadata, batch_size=2)
    expected = [hashlib.md5(text.encode()).hexdigest()[:16] for text in chunks]
    assert result == expected
    assert calls == [
        (chunks[:2], metadata[:2], expected[:2]),
        (chunks[2:], metadata[2:], expected[2:]),
    ]
    assert set(invalidated) == {1, 2}


def test_add_documents_preserves_explicit_ids(monkeypatch):
    import rag.vector_store as store

    received = []

    class FakeStore:
        def add_texts(self, **kwargs):
            received.append(kwargs)
            return kwargs["ids"]

    monkeypatch.setattr(store, "get_vector_store", lambda: FakeStore())
    monkeypatch.setattr(store, "_invalidate_retrieval_caches", lambda _owner: None)
    assert store.add_documents(["text"], [{"owner_id": 2}], ids=["caller-id"]) == ["caller-id"]
    assert received[0]["ids"] == ["caller-id"]


def test_empty_add_is_a_noop_and_failed_later_batch_still_invalidates_cache(monkeypatch):
    import rag.vector_store as store

    monkeypatch.setattr(store, "get_vector_store", lambda: pytest.fail("empty add should not open store"))
    assert store.add_documents([], []) == []

    invalidated = []
    calls = []

    class Store:
        def add_texts(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise RuntimeError("second batch failed")
            return kwargs["ids"]

    monkeypatch.setattr(store, "get_vector_store", lambda: Store())
    monkeypatch.setattr(store, "_invalidate_retrieval_caches", invalidated.append)
    with pytest.raises(RuntimeError, match="second batch failed"):
        store.add_documents(
            ["first", "second"],
            [{"owner_id": 7}, {"owner_id": 7}],
            batch_size=1,
        )
    assert len(calls) == 2
    assert invalidated == [7]


def test_search_filters_by_owner_and_wraps_storage_failures(monkeypatch):
    import rag.vector_store as store

    document = SimpleNamespace(page_content="answer", metadata={"source": "a.md"})
    calls = []

    class FakeStore:
        def similarity_search_with_score(self, query, *, k, filter):
            calls.append((query, k, filter))
            return [(document, 0.125)]

    monkeypatch.setattr(store, "get_vector_store", lambda: FakeStore())
    assert store.search_by_owner("question", 9, top_k=3) == [
        {"content": "answer", "metadata": {"source": "a.md"}, "score": 0.125}
    ]
    assert calls == [("question", 3, {"owner_id": 9})]

    def unavailable():
        raise RuntimeError("storage offline")

    monkeypatch.setattr(store, "get_vector_store", unavailable)
    with pytest.raises(store.VectorStoreError, match="storage offline") as exc:
        store.search_by_owner("question", 9)
    assert isinstance(exc.value.__cause__, RuntimeError)


def test_delete_helpers_scope_queries_and_invalidate_even_when_nothing_matches(monkeypatch):
    import rag.vector_store as store

    class Collection:
        def __init__(self, ids):
            self.ids = ids
            self.queries = []
            self.deleted = []

        def get(self, **kwargs):
            self.queries.append(kwargs)
            return {"ids": self.ids}

        def delete(self, *, ids):
            self.deleted.append(ids)

    collection = Collection(["a", "b"])
    invalidated = []
    monkeypatch.setattr(store, "_get_or_create_collection", lambda: collection)
    monkeypatch.setattr(store, "_invalidate_retrieval_caches", invalidated.append)

    assert store.delete_by_file("report.md", 4) == 2
    assert collection.queries[0] == {
        "where": {"$and": [{"owner_id": 4}, {"source": "report.md"}]}
    }
    assert collection.deleted == [["a", "b"]]

    collection.ids = []
    assert store.delete_by_file("missing.md", 4) == 0
    assert store.delete_all_by_owner(8) == 0
    assert collection.queries[-1] == {"where": {"owner_id": 8}}
    collection.ids = ["c"]
    assert store.delete_all_by_owner(8) == 1
    assert collection.deleted[-1] == ["c"]
    assert invalidated == [4, 4, 8, 8]


def test_delete_storage_errors_propagate_and_invalidate_caches(monkeypatch):
    import rag.vector_store as store

    class BrokenCollection:
        def get(self, **_kwargs):
            raise RuntimeError("collection unavailable")

    invalidated = []
    monkeypatch.setattr(store, "_get_or_create_collection", lambda: BrokenCollection())
    monkeypatch.setattr(store, "_invalidate_retrieval_caches", invalidated.append)
    with pytest.raises(store.VectorStoreError, match="collection unavailable"):
        store.delete_by_file("report.md", 2)
    with pytest.raises(store.VectorStoreError, match="collection unavailable"):
        store.delete_all_by_owner(3)
    assert invalidated == [2, 3]


def test_partial_delete_failure_is_visible_and_invalidates_cache(monkeypatch):
    import rag.vector_store as store

    class Collection:
        def get(self, **_kwargs):
            return {"ids": ["chunk"]}

        def delete(self, **_kwargs):
            raise RuntimeError("delete interrupted")

    invalidated = []
    monkeypatch.setattr(store, "_get_or_create_collection", lambda: Collection())
    monkeypatch.setattr(store, "_invalidate_retrieval_caches", invalidated.append)
    with pytest.raises(store.VectorStoreError, match="delete interrupted"):
        store.delete_by_file("report.md", 2)
    assert invalidated == [2]


def test_get_or_create_collection_uses_configured_client(monkeypatch):
    import rag.vector_store as store

    collection = object()
    calls = []

    class Client:
        def get_or_create_collection(self, name):
            calls.append(name)
            return collection

    monkeypatch.setattr(store, "_get_chroma_client", lambda: Client())
    assert store._get_or_create_collection() is collection
    assert calls == [store.COLLECTION_NAME]


def test_stats_deduplicate_and_sort_sources_and_preserve_empty_metadata(monkeypatch):
    import rag.vector_store as store

    class Collection:
        def get(self, **kwargs):
            assert kwargs == {"where": {"owner_id": 5}, "include": ["metadatas"]}
            return {
                "ids": ["1", "2", "3", "4"],
                "metadatas": [{"source": "z.md"}, {"source": "a.md"}, {}, None],
            }

    monkeypatch.setattr(store, "_get_or_create_collection", lambda: Collection())
    assert store.get_collection_stats(5) == {
        "total_chunks": 4,
        "unique_files": 2,
        "files": ["a.md", "z.md"],
        "error": None,
    }


def test_stats_failure_is_distinguishable_from_an_empty_collection(monkeypatch):
    import rag.vector_store as store

    monkeypatch.setattr(
        store,
        "_get_or_create_collection",
        lambda: (_ for _ in ()).throw(RuntimeError("stats unavailable")),
    )
    result = store.get_collection_stats(6)
    assert result["total_chunks"] == 0
    assert result["error"] == "获取向量库统计失败: stats unavailable"


def test_list_chunks_sorts_skips_missing_metadata_and_zips_short_results(monkeypatch):
    import rag.vector_store as store

    class Collection:
        def get(self, **kwargs):
            assert kwargs == {
                "where": {"$and": [{"owner_id": 3}, {"source": "guide.md"}]},
                "include": ["metadatas", "documents"],
            }
            return {
                "metadatas": [
                    {"chunk_idx": 4, "page": 2},
                    None,
                    {"chunk_idx": "invalid", "page": None},
                    {"chunk_idx": 1},
                ],
                "documents": ["fourth", "skip", "fallback-index", "first", "extra ignored"],
            }

    monkeypatch.setattr(store, "_get_or_create_collection", lambda: Collection())
    assert store.list_chunks("guide.md", 3, limit=0, offset=0) == {
        "total": 3,
        "chunks": [
            {"chunk_idx": 0, "page": None, "content": "fallback-index", "chars": 14}
        ],
    }
    page = store.list_chunks("guide.md", 3, limit=2, offset=1)
    assert [chunk["chunk_idx"] for chunk in page["chunks"]] == [1, 4]
    assert page["total"] == 3


def test_lazy_clients_are_cached_and_vector_store_reset_keeps_client(monkeypatch, tmp_path):
    import rag.vector_store as store

    client = object()
    calls = []
    monkeypatch.setattr(store, "_chroma_client", None)
    monkeypatch.setattr(store, "_vector_store", None)
    monkeypatch.setattr(
        store,
        "settings",
        SimpleNamespace(
            chroma_persist_dir="chroma",
            resolve_path=lambda _path: tmp_path / "chroma",
        ),
    )
    monkeypatch.setattr(store.os, "makedirs", lambda path, exist_ok: calls.append((path, exist_ok)))
    monkeypatch.setattr(
        store.chromadb,
        "PersistentClient",
        lambda **kwargs: calls.append(kwargs) or client,
    )

    assert store._get_chroma_client() is client
    assert store._get_chroma_client() is client
    assert calls[0] == (str(tmp_path / "chroma"), True)
    assert calls[1]["path"] == str(tmp_path / "chroma")

    monkeypatch.setattr(store, "_vector_store", object())
    store.reset_vector_store()
    assert store._vector_store is None
    assert store._chroma_client is client


def test_vector_store_uses_injected_embedding_and_caches(monkeypatch):
    import rag.vector_store as store

    client = object()
    embeddings = object()
    created = []

    class Context:
        class Embed:
            @staticmethod
            def embeddings():
                return embeddings

        embed = Embed()

    monkeypatch.setattr(store, "_chroma_client", None)
    monkeypatch.setattr(store, "_vector_store", None)
    monkeypatch.setattr(store, "_get_chroma_client", lambda: client)
    monkeypatch.setattr(store, "get_context", lambda: Context())
    monkeypatch.setattr(
        store,
        "Chroma",
        lambda **kwargs: created.append(kwargs) or "vector-store",
    )

    assert store.get_vector_store() == "vector-store"
    assert store.get_vector_store() == "vector-store"
    assert created == [
        {"client": client, "collection_name": store.COLLECTION_NAME, "embedding_function": embeddings}
    ]


def test_cache_invalidation_failures_do_not_block_other_cache(monkeypatch):
    import rag.bm25_search as bm25
    import rag.search_cache as search_cache
    import rag.vector_store as store

    cleared = []

    def bm25_fails(_owner):
        raise RuntimeError("bm25 cache unavailable")

    monkeypatch.setattr(bm25, "invalidate_bm25_cache", bm25_fails)
    monkeypatch.setattr(search_cache, "clear_cache", lambda: cleared.append("search"))
    store._invalidate_retrieval_caches(1)
    assert cleared == ["search"]

    invalidated = []
    monkeypatch.setattr(bm25, "invalidate_bm25_cache", invalidated.append)

    def cache_fails():
        raise RuntimeError("search cache unavailable")

    monkeypatch.setattr(search_cache, "clear_cache", cache_fails)
    store._invalidate_retrieval_caches(2)
    assert invalidated == [2]
