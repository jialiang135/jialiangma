"""Batching, ordering, retries, and upstream failures for the embedding client."""

from types import SimpleNamespace

import pytest


def test_batch_size_defaults_and_explicit_override():
    from core.embeddings import DEFAULT_BATCH_SIZE, DashScopeEmbeddings

    assert DashScopeEmbeddings("text-embedding-v1", "key").batch_size == 25
    assert DashScopeEmbeddings("text-embedding-v4", "key").batch_size == 10
    assert DashScopeEmbeddings("custom-model", "key").batch_size == DEFAULT_BATCH_SIZE
    assert DashScopeEmbeddings("text-embedding-v4", "key", batch_size=3).batch_size == 3


def test_embed_documents_chunks_batches_and_replaces_blank_inputs(monkeypatch):
    from core.embeddings import DashScopeEmbeddings

    provider = DashScopeEmbeddings("text-embedding-v4", "key", batch_size=2)
    calls = []

    def embed(batch):
        calls.append(batch)
        return [[float(len(calls)), float(index)] for index, _text in enumerate(batch)]

    monkeypatch.setattr(provider, "_embed_with_retry", embed)
    result = provider.embed_documents(["first", "", "  ", "fourth", "fifth"])
    assert calls == [["first", " "], [" ", "fourth"], ["fifth"]]
    assert len(result) == 5


def test_empty_documents_and_blank_query_have_defined_behavior(monkeypatch):
    from core.embeddings import DashScopeEmbeddings

    provider = DashScopeEmbeddings("text-embedding-v4", "key")
    calls = []
    monkeypatch.setattr(provider, "_embed_with_retry", lambda texts: calls.append(texts) or [[1.0]])
    assert provider.embed_documents([]) == []
    assert calls == []
    assert provider.embed_query(" \n") == [1.0]
    assert calls == [[" "]]


def test_batch_call_sorts_by_text_index_and_uses_circuit_breaker(monkeypatch):
    import dashscope

    import core.circuit_breaker as circuits
    from core.embeddings import DashScopeEmbeddings

    response = SimpleNamespace(
        status_code=200,
        output={
            "embeddings": [
                {"text_index": 1, "embedding": [2.0]},
                {"text_index": 0, "embedding": [1.0]},
            ]
        },
        message="ok",
    )
    calls = []
    monkeypatch.setattr(dashscope.TextEmbedding, "call", lambda **kwargs: calls.append(kwargs) or response)
    provider = DashScopeEmbeddings("text-embedding-v4", "secret")

    circuit_calls = []

    def call_sync(func, *args):
        circuit_calls.append((func, args))
        return func(*args)

    monkeypatch.setattr(circuits.embedding_circuit_breaker, "call_sync", call_sync)
    assert provider._call_batch(["a", "b"]) == [[1.0], [2.0]]
    assert calls[0] == {"api_key": "secret", "model": "text-embedding-v4", "input": ["a", "b"]}
    assert circuit_calls[0][0] == provider._call_batch_once
    assert circuit_calls[0][1] == (["a", "b"],)


def test_batch_call_rejects_provider_status_and_count_mismatch(monkeypatch):
    import dashscope

    from core.embeddings import DashScopeEmbeddings, EmbeddingError

    provider = DashScopeEmbeddings("model", "key")
    monkeypatch.setattr(
        dashscope.TextEmbedding,
        "call",
        lambda **_kwargs: SimpleNamespace(status_code=429, message="busy", output=None),
    )
    with pytest.raises(EmbeddingError, match="429 - busy"):
        provider._call_batch_once(["a"])

    monkeypatch.setattr(
        dashscope.TextEmbedding,
        "call",
        lambda **_kwargs: SimpleNamespace(
            status_code=200,
            message="ok",
            output={"embeddings": [{"text_index": 0, "embedding": [1.0]}]},
        ),
    )
    with pytest.raises(EmbeddingError, match="期望 2, 实际 1"):
        provider._call_batch_once(["a", "b"])


def test_tenacity_retries_transient_failures_and_stops_at_limit(monkeypatch):
    from tenacity import wait_none

    import core.embeddings as embeddings

    monkeypatch.setattr(embeddings, "wait_exponential", lambda **_kwargs: wait_none())
    provider = embeddings.DashScopeEmbeddings("model", "key", max_retries=3)
    attempts = []

    def recover(texts):
        attempts.append(texts)
        if len(attempts) < 3:
            raise RuntimeError("temporary upstream failure")
        return [[0.1, 0.2]]

    monkeypatch.setattr(provider, "_call_batch", recover)
    assert provider._embed_with_retry(["text"]) == [[0.1, 0.2]]
    assert len(attempts) == 3

    attempts.clear()
    provider.max_retries = 2

    def always_fails(texts):
        attempts.append(texts)
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(provider, "_call_batch", always_fails)
    with pytest.raises(RuntimeError, match="upstream unavailable"):
        provider._embed_with_retry(["text"])
    assert len(attempts) == 2
