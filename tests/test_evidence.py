"""
证据轨（evidence）测试
======================

覆盖三处改动，全部离线（不调 DashScope / LLM / 真实向量库）：

1. **retriever 组装**：documents 必须带出 ``chunk_idx`` / ``filepath``，
   且已有的 ``content`` / ``score`` / ``source`` 键名不变（向后兼容）。
2. **证据数组构造** ``_build_evidence``：正文截断到 800 字 + ``…``、
   ``chunk_idx`` 取不到给 ``None``、顺序与输入一致。
3. **SSE 事件**：真实跑一遍 ``sse_chat_generator``（用假图喂事件），
   断下发的 ``evidence`` 事件是合法 JSON 且结构正确；顺带验证
   ``reasoning`` 落库 JSON 里带了同一份 evidence。

参考 ``tests/test_tts.py`` 的写法：把外部依赖换掉，只做纯逻辑断言。
"""

import asyncio
import json

import pytest

# ============================================================
# 1. retriever 组装
# ============================================================


class TestRetrieverCarriesChunkIdx:
    def _patch(self, monkeypatch, raw_results, rerank_order=None):
        """把向量检索与 rerank 换成本地实现（不联网）。"""
        import rag.retriever as retriever

        def fake_rerank(query, documents, top_n=5):
            order = rerank_order if rerank_order is not None else range(len(documents))
            return [
                {"index": i, "score": 0.9 - 0.1 * n, "text": documents[i]}
                for n, i in enumerate(order)
            ]

        monkeypatch.setattr(retriever, "search_by_owner", lambda *a, **k: raw_results)
        monkeypatch.setattr(retriever, "rerank_with_dashscope", fake_rerank)
        return retriever

    def test_backward_compatible_and_carries_new_fields(self, monkeypatch):
        """既有三键不变，新增 chunk_idx / filepath。"""
        raw = [
            {
                "content": "第一段",
                "metadata": {
                    "source": "doc.md",
                    "chunk_idx": 0,
                    "filepath": "/data/doc.md",
                    "owner_id": 1,
                },
                "score": 0.9,
            },
            {
                "content": "第二段",
                "metadata": {"source": "doc.md", "chunk_idx": 1, "filepath": "/data/doc.md"},
                "score": 0.5,
            },
        ]
        retriever = self._patch(monkeypatch, raw)

        result = retriever._retrieve_impl(
            "q", 1, top_k_search=10, top_k_rerank=5, use_hybrid=False, bm25_weight=0.3
        )

        assert result["count"] == 2
        d0 = result["documents"][0]
        # 原有键名一个都不能少
        assert {"content", "score", "source"} <= set(d0)
        assert d0["content"] == "第一段"
        assert d0["score"] == 0.9
        assert d0["source"] == "doc.md"
        # 新增键
        assert d0["chunk_idx"] == 0  # 0 是合法值，不能被当成 falsy 丢掉
        assert d0["filepath"] == "/data/doc.md"
        assert result["documents"][1]["chunk_idx"] == 1

    def test_missing_metadata_fields_do_not_crash(self, monkeypatch):
        """旧数据 / 工具路径没有这些 metadata 字段时，兜底为 None。"""
        raw = [{"content": "旧片段", "metadata": {"source": "old.txt"}, "score": 0.8}]
        retriever = self._patch(monkeypatch, raw)

        result = retriever._retrieve_impl(
            "q", 1, top_k_search=10, top_k_rerank=5, use_hybrid=False, bm25_weight=0.3
        )

        doc = result["documents"][0]
        assert doc["chunk_idx"] is None
        assert doc["filepath"] is None
        assert doc["source"] == "old.txt"


# ============================================================
# 2. 证据数组构造
# ============================================================


class TestBuildEvidence:
    def test_truncates_long_content_with_ellipsis(self):
        from api.sse_stream import MAX_EVIDENCE_CONTENT, _build_evidence

        long_text = "字" * (MAX_EVIDENCE_CONTENT + 200)
        out = _build_evidence(
            [{"content": long_text, "score": 0.389, "source": "a.md", "chunk_idx": 3}]
        )
        assert len(out) == 1
        assert len(out[0]["content"]) == MAX_EVIDENCE_CONTENT + 1  # 800 字 + "…"
        assert out[0]["content"].endswith("…")

    def test_short_content_kept_intact(self):
        from api.sse_stream import _build_evidence

        out = _build_evidence([{"content": "短正文", "score": 0.3, "source": "b.md"}])
        assert out[0]["content"] == "短正文"

    def test_missing_chunk_idx_is_none_and_zero_preserved(self):
        from api.sse_stream import _build_evidence

        out = _build_evidence(
            [
                {"content": "一", "score": 0.9, "source": "x.md", "chunk_idx": 0},
                {"content": "二", "score": 0.8, "source": "y.md"},  # 缺 chunk_idx
            ]
        )
        assert out[0]["chunk_idx"] == 0
        assert out[1]["chunk_idx"] is None

    def test_score_stays_numeric(self):
        from api.sse_stream import _build_evidence

        out = _build_evidence([{"content": "c", "score": 0.389, "source": "s.md", "chunk_idx": 1}])
        assert isinstance(out[0]["score"], float)
        assert out[0]["score"] == 0.389

    def test_order_matches_input(self):
        from api.sse_stream import _build_evidence

        docs = [
            {"content": "a", "score": 0.9, "source": "1.md", "chunk_idx": 0},
            {"content": "b", "score": 0.8, "source": "2.md", "chunk_idx": 1},
            {"content": "c", "score": 0.7, "source": "3.md", "chunk_idx": 2},
        ]
        out = _build_evidence(docs)
        assert [d["source"] for d in out] == ["1.md", "2.md", "3.md"]

    def test_non_dict_entries_skipped(self):
        from api.sse_stream import _build_evidence

        out = _build_evidence(
            [None, "junk", {"content": "ok", "score": 0.1, "source": "s.md", "chunk_idx": 2}]
        )
        assert len(out) == 1
        assert out[0]["source"] == "s.md"

    def test_empty_input(self):
        from api.sse_stream import _build_evidence

        assert _build_evidence([]) == []


# ============================================================
# 3. SSE 事件（跑真实生成器，喂假图事件）
# ============================================================


class _FakeGraph:
    """假 Agent 图：把预置事件按顺序吐出来。"""

    def __init__(self, events):
        self._events = events

    async def astream_events(self, state, version="v2"):
        for event in self._events:
            yield event


def _drain(agen):
    """在同步测试里把异步生成器收干。"""

    async def _collect():
        return [chunk async for chunk in agen]

    return asyncio.run(_collect())


def _evidence_events(chunks):
    """从 SSE 文本块里挑出 evidence 事件并解析。"""
    events = []
    for chunk in chunks:
        assert chunk.startswith("data: ")
        payload = json.loads(chunk[len("data: ") :].strip())
        if payload["type"] == "evidence":
            events.append(payload)
    return events


def _run_generator(monkeypatch, docs):
    import api.sse_stream as sse

    # 落库不需要真跑：换成异步 no-op（工厂模式，返回新协程），
    # 同时把传给 _persist_turn 的参数记下来，验证闭包确实带上了 evidence。
    captured: dict = {}

    async def _noop():
        return None

    def _fake_persist(**kw):
        captured.update(kw)
        return _noop()

    monkeypatch.setattr(sse, "_persist_turn", _fake_persist)

    events = [
        {
            "event": "on_chain_end",
            "name": "retrieve",
            "data": {"output": {"retrieved_docs": docs}},
        },
        {
            "event": "on_chain_end",
            "name": "chat_agent",
            "data": {"output": {"final_answer": "这是回答", "reasoning_log": []}},
        },
    ]
    monkeypatch.setattr(sse, "get_agent_graph", lambda: _FakeGraph(events))

    gen = sse.sse_chat_generator(user_query="你好", owner_id=1)
    return _drain(gen), captured


class TestEvidenceSseEvent:
    def test_emits_valid_json_with_expected_structure(self, monkeypatch):
        docs = [
            {
                "content": "甲" * 1000,
                "score": 0.389,
                "source": "01_自我介绍.md",
                "chunk_idx": 3,
                "filepath": "/data/01_自我介绍.md",
            },
            {"content": "短片段", "score": 0.21, "source": "02.md"},  # 缺 chunk_idx
        ]
        chunks, _ = _run_generator(monkeypatch, docs)

        evidence_events = _evidence_events(chunks)
        assert len(evidence_events) == 1  # 一次检索只发一条

        payload = evidence_events[0]
        assert set(payload) == {"type", "content"}
        items = json.loads(payload["content"])
        assert isinstance(items, list)
        assert len(items) == 2

        first = items[0]
        assert set(first) == {"source", "score", "content", "chunk_idx"}
        assert first["source"] == "01_自我介绍.md"
        assert first["score"] == 0.389  # 数值，不是格式化字符串
        assert first["chunk_idx"] == 3
        assert first["content"].endswith("…")
        assert len(first["content"]) == 801

        second = items[1]
        assert second["source"] == "02.md"
        assert second["chunk_idx"] is None  # 取不到 → null，不报错
        assert second["content"] == "短片段"

        # 顺序与 retrieved_docs 一致
        assert [i["source"] for i in items] == ["01_自我介绍.md", "02.md"]

    def test_empty_retrieval_still_emits_empty_array(self, monkeypatch):
        chunks, _ = _run_generator(monkeypatch, [])
        evidence_events = _evidence_events(chunks)
        assert len(evidence_events) == 1
        assert json.loads(evidence_events[0]["content"]) == []

    def test_generator_passes_evidence_into_persistence(self, monkeypatch):
        """落库闭包必须拿到同一份证据数组（前端恢复历史时用它）。"""
        from api.sse_stream import _build_evidence

        docs = [
            {"content": "正文", "score": 0.42, "source": "x.md", "chunk_idx": 2},
        ]
        _, captured = _run_generator(monkeypatch, docs)
        assert captured["evidence"] == _build_evidence(docs)
        assert captured["sources"] == ["x.md"]

    def test_event_line_format(self):
        """结构化事件的线格式：data: {type, content} + 空行。"""
        from api.sse_stream import _sse_event

        line = _sse_event("evidence", json.dumps([], ensure_ascii=False))
        assert line.startswith("data: ")
        assert line.endswith("\n\n")
        payload = json.loads(line[len("data: ") :].strip())
        assert payload == {"type": "evidence", "content": "[]"}


# ============================================================
# 4. 落库 reasoning JSON 带 evidence
# ============================================================


class TestPersistTurnEvidence:
    def test_reasoning_json_contains_evidence(self, monkeypatch):
        import core.database as db

        captured: dict = {}

        async def fake_insert_chat_log(**kwargs):
            captured.update(kwargs)
            return 1

        monkeypatch.setattr(db, "insert_chat_log", fake_insert_chat_log)

        from api.sse_stream import _persist_turn
        from tests.conftest import run_async

        evidence = [
            {"source": "a.md", "score": 0.5, "content": "正文", "chunk_idx": None},
        ]
        run_async(
            _persist_turn(
                owner_id=1,
                agent_mode="chat",
                question="q",
                answer="a",
                steps=["步骤一"],
                thinking="思考",
                sources=["a.md"],
                evidence=evidence,
                conversation_id="cid",
                usage={},
            )
        )

        reasoning = json.loads(captured["reasoning"])
        # steps / thinking 原样保留
        assert reasoning["steps"] == ["步骤一"]
        assert reasoning["thinking"] == "思考"
        # 新增 evidence，与 SSE 同款结构
        assert reasoning["evidence"] == evidence
        # sources 维持文件名数组不变（还有别处在用）
        assert json.loads(captured["sources"]) == ["a.md"]

    def test_evidence_uses_passed_in_truncated_array(self, monkeypatch):
        """落库存的是构造好的（已截断）数组，不再二次处理。"""
        import core.database as db

        captured: dict = {}

        async def fake_insert_chat_log(**kwargs):
            captured.update(kwargs)
            return 1

        monkeypatch.setattr(db, "insert_chat_log", fake_insert_chat_log)

        from api.sse_stream import _build_evidence, _persist_turn
        from tests.conftest import run_async

        long_text = "字" * 2000
        evidence = _build_evidence(
            [{"content": long_text, "score": 0.1, "source": "big.md", "chunk_idx": 5}]
        )

        run_async(
            _persist_turn(
                owner_id=1,
                agent_mode="chat",
                question="q",
                answer="a",
                steps=[],
                thinking="",
                sources=["big.md"],
                evidence=evidence,
                conversation_id="cid",
                usage={},
            )
        )

        reasoning = json.loads(captured["reasoning"])
        assert len(reasoning["evidence"][0]["content"]) == 801
        assert reasoning["evidence"][0]["content"].endswith("…")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
