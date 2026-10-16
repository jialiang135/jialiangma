"""BM25 construction, filtering, ranking, and hybrid-fusion failure paths."""

import rag.bm25_search as bm25


def test_build_index_handles_client_collection_and_empty_corpus_failures(monkeypatch):
    class Client:
        def get_collection(self, _name):
            raise RuntimeError("collection unavailable")

    monkeypatch.setattr(bm25, "_get_chroma_client", lambda: Client())
    assert bm25.BM25IndexManager().build_index(1) == (None, [])

    class Collection:
        def get(self, **_kwargs):
            raise RuntimeError("query failed")

    class ClientWithCollection:
        def get_collection(self, _name):
            return Collection()

    monkeypatch.setattr(bm25, "_get_chroma_client", lambda: ClientWithCollection())
    assert bm25.BM25IndexManager().build_index(1) == (None, [])

    class EmptyCollection:
        def get(self, **_kwargs):
            return {"ids": [], "documents": [], "metadatas": []}

    class ClientWithEmptyCollection:
        def get_collection(self, _name):
            return EmptyCollection()

    monkeypatch.setattr(bm25, "_get_chroma_client", lambda: ClientWithEmptyCollection())
    assert bm25.BM25IndexManager().build_index(1) == (None, [])


def test_build_index_defaults_missing_metadata_and_skips_empty_documents(monkeypatch):
    class Collection:
        documents = None

        def get(self, **_kwargs):
            return {"ids": ["1", "2"], "documents": self.documents, "metadatas": None}

    collection = Collection()

    class Client:
        def get_collection(self, _name):
            return collection

    monkeypatch.setattr(bm25, "_get_chroma_client", lambda: Client())
    manager = bm25.BM25IndexManager()
    index, documents = manager.build_index(1)
    assert index is None
    assert documents == []

    collection.documents = ["valid searchable text", ""]
    index, documents = manager.build_index(1)
    assert index is not None
    assert documents == [{"doc_id": "1", "content": "valid searchable text", "metadata": {}}]


def test_vector_search_survives_a_corpus_with_no_keyword_tokens(monkeypatch):
    manager = bm25.BM25IndexManager()

    class Collection:
        def get(self, **_kwargs):
            return {"ids": ["empty"], "documents": ["   😀"], "metadatas": [{}]}

    class Client:
        def get_collection(self, _name):
            return Collection()

    vector_result = [{"content": "vector answer", "metadata": {"source": "doc.md"}, "score": 0.9}]
    monkeypatch.setattr(bm25, "_bm25_manager", manager)
    monkeypatch.setattr(bm25, "_get_chroma_client", lambda: Client())
    monkeypatch.setattr(bm25, "search_by_owner", lambda *_args, **_kwargs: vector_result)

    assert bm25.hybrid_search("query", 1) == [
        {"content": "vector answer", "metadata": {"source": "doc.md"}, "score": 0.7 / 61}
    ]


def test_failed_index_is_not_cached_and_blank_queries_skip_lookup(monkeypatch):
    manager = bm25.BM25IndexManager()
    calls = []
    monkeypatch.setattr(manager, "build_index", lambda owner: calls.append(owner) or (None, []))
    assert manager.get_index(7) == (None, [])
    assert manager.get_index(7) == (None, [])
    assert calls == [7, 7]

    lookup_calls = []
    monkeypatch.setattr(bm25._bm25_manager, "get_index", lambda owner: lookup_calls.append(owner))
    assert bm25.bm25_search("   ", 7) == []
    assert lookup_calls == []


def test_bm25_search_handles_missing_index_empty_tokens_no_hits_and_top_k(monkeypatch):
    monkeypatch.setattr(bm25._bm25_manager, "get_index", lambda _owner: (None, []))
    assert bm25.bm25_search("query", 1) == []

    documents = [
        {"doc_id": "first", "content": "first text", "metadata": {"source": "a"}},
        {"doc_id": "second", "content": "second text", "metadata": {"source": "b"}},
        {"doc_id": "third", "content": "third text", "metadata": {"source": "c"}},
    ]

    class Index:
        def get_scores(self, _tokens):
            return [0.2, 0.9, 0.9]

    monkeypatch.setattr(bm25._bm25_manager, "get_index", lambda _owner: (Index(), documents))
    assert bm25.bm25_search("!!!", 1) == []
    assert bm25.bm25_search("query", 1, top_k=0) == []
    results = bm25.bm25_search("query", 1, top_k=2)
    assert [row["doc_id"] for row in results] == ["second", "third"]
    assert results[0]["score"] == 0.9


def test_hybrid_search_handles_blank_empty_vector_only_and_keyword_only(monkeypatch):
    calls = []
    monkeypatch.setattr(bm25, "search_by_owner", lambda *args, **kwargs: calls.append((args, kwargs)) or [])
    monkeypatch.setattr(bm25, "bm25_search", lambda *args, **kwargs: calls.append((args, kwargs)) or [])
    assert bm25.hybrid_search("  ", 1) == []
    assert calls == []
    assert bm25.hybrid_search("query", 1) == []
    assert len(calls) == 2
    assert calls[0][1]["top_k"] == 15

    vector = [{"content": "vector result", "metadata": {"source": "vector.md"}, "score": 0.99}]
    monkeypatch.setattr(bm25, "search_by_owner", lambda *_args, **_kwargs: vector)
    monkeypatch.setattr(bm25, "bm25_search", lambda *_args, **_kwargs: [])
    vector_only = bm25.hybrid_search("query", 1, top_k=1)
    assert vector_only[0]["content"] == "vector result"
    assert vector_only[0]["metadata"] == {"source": "vector.md"}

    keyword = [{"content": "keyword result", "metadata": {"source": "keyword.md"}, "score": 5}]
    monkeypatch.setattr(bm25, "search_by_owner", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(bm25, "bm25_search", lambda *_args, **_kwargs: keyword)
    keyword_only = bm25.hybrid_search("query", 1, top_k=1, bm25_weight=0.25)
    assert keyword_only[0]["content"] == "keyword result"
    assert keyword_only[0]["score"] == 0.25 / 61


def test_hybrid_fusion_combines_duplicates_enriches_metadata_and_limits_results(monkeypatch):
    vector = [
        {"content": "shared", "metadata": {}, "score": 0.8},
        {"content": "vector-only", "metadata": {"source": "v.md"}, "score": 0.7},
    ]
    keyword = [
        {"content": "shared", "metadata": {"source": "bm.md"}, "score": 2.0},
        {"content": "keyword-only", "metadata": {"source": "k.md"}, "score": 1.0},
    ]
    monkeypatch.setattr(bm25, "search_by_owner", lambda *_args, **_kwargs: vector)
    monkeypatch.setattr(bm25, "bm25_search", lambda *_args, **_kwargs: keyword)
    result = bm25.hybrid_search("query", 1, top_k=2, bm25_weight=0.3)

    assert len(result) == 2
    shared = next(row for row in result if row["content"] == "shared")
    assert shared["metadata"] == {"source": "bm.md"}
    assert shared["score"] == (0.7 / 61) + (0.3 / 61)
    assert vector[0]["metadata"] == {}  # fusion must not mutate source metadata
    assert all(row["content"] != "keyword-only" for row in result)
