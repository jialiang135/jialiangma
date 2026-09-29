"""
检索链路健壮性测试
==================
覆盖本次修的一批 RAG 检索链路真实缺陷，**全部离线**（不调 DashScope / LLM /
真实向量库，需要外部依赖的地方一律 monkeypatch 掉）。参考 ``tests/test_evidence.py``
的写法：把外部依赖换成本地实现，只做纯逻辑断言。

覆盖点：
1. **相关性阈值过滤** —— 低于 ``settings.retrieval_min_score`` 的结果被丢弃，
   ``filtered_count`` 正确；阈值可配、设为 0 等于关闭。
2. **全被过滤** —— ``documents`` 为空但 ``error`` 为 ``None``（表示"确实没有
   相关内容"，不是链路故障）。
3. **rerank 降级** —— ``degraded=True``，``score`` 为 ``None``（**不是伪造的
   1.0**），且阈值在降级时**不生效**。
4. **检索故障** —— 向量库抛异常时 ``retrieve`` 的 ``error`` 有值，而不是伪装成
   空结果；``get_collection_stats`` 失败时带出 ``error`` 键。
5. **分块重叠语义（B 方案）** —— 语义分块路径不做重叠，``chunk_overlap`` 只作用
   于定长兜底路径；``deduplicate_chunks`` 不再有死参数 ``threshold``。
"""

import inspect

import pytest

# ============================================================
# 工具
# ============================================================


def _patch(monkeypatch, raw_results, rerank_items):
    """把向量检索与 rerank 换成本地实现（不联网），返回被 patch 的 retriever 模块。"""
    import rag.retriever as retriever

    monkeypatch.setattr(retriever, "search_by_owner", lambda *a, **k: raw_results)
    monkeypatch.setattr(retriever, "rerank_with_dashscope", lambda *a, **k: rerank_items)
    return retriever


def _raw(n):
    """构造 n 条向量检索原始结果（带 metadata）。"""
    return [
        {"content": f"内容{i}", "metadata": {"source": "s.md", "chunk_idx": i}, "score": 0.9}
        for i in range(n)
    ]


def _rerank(scores, degraded=False):
    """按给定分数构造 rerank 输出（degraded 时 score 强制为 None）。"""
    return [
        {"index": i, "score": None if degraded else s, "text": f"内容{i}", "degraded": degraded}
        for i, s in enumerate(scores)
    ]


# ============================================================
# 1. 相关性阈值过滤
# ============================================================


class TestScoreThreshold:
    def test_low_score_dropped_and_counted(self, monkeypatch):
        retriever = _patch(
            monkeypatch,
            _raw(3),
            [
                {"index": 0, "score": 0.5, "text": "内容0"},
                {"index": 1, "score": 0.01, "text": "内容1"},  # 低于默认 0.05
                {"index": 2, "score": 0.4, "text": "内容2"},
            ],
        )
        result = retriever._retrieve_impl("q", 1, 10, 5, use_hybrid=False, bm25_weight=0.3)

        assert result["count"] == 2
        assert result["filtered_count"] == 1
        assert result["error"] is None
        assert result["degraded"] is False
        # 被丢的是 index=1 那条
        assert [d["content"] for d in result["documents"]] == ["内容0", "内容2"]
        # 留下的仍是真实分数
        assert result["documents"][0]["score"] == 0.5

    def test_threshold_is_configurable(self, monkeypatch):
        retriever = _patch(
            monkeypatch,
            _raw(3),
            [
                {"index": 0, "score": 0.5, "text": "内容0"},
                {"index": 1, "score": 0.4, "text": "内容1"},
                {"index": 2, "score": 0.3, "text": "内容2"},
            ],
        )
        monkeypatch.setattr(retriever.settings, "retrieval_min_score", 0.45)
        result = retriever._retrieve_impl("q", 1, 10, 5, use_hybrid=False, bm25_weight=0.3)

        assert result["count"] == 1
        assert result["filtered_count"] == 2

    def test_threshold_zero_disables_filtering(self, monkeypatch):
        retriever = _patch(
            monkeypatch,
            _raw(3),
            [
                {"index": 0, "score": 0.5, "text": "内容0"},
                {"index": 1, "score": 0.001, "text": "内容1"},
                {"index": 2, "score": 0.0, "text": "内容2"},
            ],
        )
        monkeypatch.setattr(retriever.settings, "retrieval_min_score", 0.0)
        result = retriever._retrieve_impl("q", 1, 10, 5, use_hybrid=False, bm25_weight=0.3)

        assert result["count"] == 3
        assert result["filtered_count"] == 0


# ============================================================
# 2. 全部被过滤
# ============================================================


class TestAllFiltered:
    def test_empty_documents_but_no_error(self, monkeypatch):
        retriever = _patch(
            monkeypatch,
            _raw(2),
            [
                {"index": 0, "score": 0.01, "text": "内容0"},
                {"index": 1, "score": 0.02, "text": "内容1"},
            ],
        )
        result = retriever._retrieve_impl("q", 1, 10, 5, use_hybrid=False, bm25_weight=0.3)

        assert result["documents"] == []
        assert result["count"] == 0
        assert result["context"] == ""
        # 关键：这是"确实没有相关内容"，不是链路故障
        assert result["error"] is None
        assert result["filtered_count"] == 2

    def test_empty_search_result_is_not_error(self, monkeypatch):
        retriever = _patch(monkeypatch, [], [])
        result = retriever._retrieve_impl("q", 1, 10, 5, use_hybrid=False, bm25_weight=0.3)
        assert result["documents"] == []
        assert result["error"] is None


# ============================================================
# 3. rerank 降级
# ============================================================


class TestRerankDegraded:
    def test_degraded_flag_and_no_fake_score(self, monkeypatch):
        retriever = _patch(monkeypatch, _raw(2), _rerank([0, 0], degraded=True))
        result = retriever._retrieve_impl("q", 1, 10, 5, use_hybrid=False, bm25_weight=0.3)

        assert result["degraded"] is True
        # score 明确表示"未评分"，绝不能是伪造的 1.0（原缺陷）
        assert all(d["score"] is None for d in result["documents"])
        assert all(d["score"] != 1.0 for d in result["documents"])
        assert result["error"] is None
        # 降级时顺序保留（index 0,1）
        assert [d["content"] for d in result["documents"]] == ["内容0", "内容1"]

    def test_threshold_skipped_when_degraded(self, monkeypatch):
        """降级时分数未知，阈值不能生效（否则会误杀）。"""
        retriever = _patch(monkeypatch, _raw(3), _rerank([0, 0, 0], degraded=True))
        # 把阈值调到极高：若降级路径错误地套用阈值，结果会被全部丢掉
        monkeypatch.setattr(retriever.settings, "retrieval_min_score", 0.99)
        result = retriever._retrieve_impl("q", 1, 10, 5, use_hybrid=False, bm25_weight=0.3)

        assert result["count"] == 3
        assert result["filtered_count"] == 0
        assert result["degraded"] is True

    def test_degraded_rerank_helper_never_writes_one(self):
        """真正跑一遍 settings 里的降级构造函数：score 必须是 None，不是 1.0。"""
        from config.settings import _degraded_rerank

        out = _degraded_rerank(["a", "b", "c", "d"], top_n=3)
        assert [item["index"] for item in out] == [0, 1, 2]  # 保留输入顺序、截断到 top_n
        assert all(item["score"] is None for item in out)
        assert all(item["degraded"] is True for item in out)
        assert all(item["score"] != 1.0 for item in out)

    def test_real_rerank_call_failure_degrades(self, monkeypatch):
        """mock dashscope 的 rerank 调用抛错，走真实 rerank_with_dashscope 的降级分支。"""
        dashscope = pytest.importorskip("dashscope")
        from config.settings import rerank_with_dashscope

        class _Boom:
            @staticmethod
            def call(**kwargs):
                raise RuntimeError("rerank service down")

        monkeypatch.setattr(dashscope, "TextReRank", _Boom)
        out = rerank_with_dashscope("q", ["a", "b"], top_n=5)

        assert all(item["score"] is None for item in out)
        assert all(item["degraded"] is True for item in out)


# ============================================================
# 4. 检索故障
# ============================================================


class TestSearchFailure:
    def test_retrieve_impl_sets_error_on_search_exception(self, monkeypatch):
        import rag.retriever as retriever
        from rag.vector_store import VectorStoreError

        def boom(*a, **k):
            raise VectorStoreError("向量检索失败: chroma 挂了")

        monkeypatch.setattr(retriever, "search_by_owner", boom)
        result = retriever._retrieve_impl("q", 1, 10, 5, use_hybrid=False, bm25_weight=0.3)

        # 关键：故障必须可感知，而不是伪装成"没检索到"
        assert result["error"] is not None
        assert "向量检索失败" in result["error"]
        assert result["documents"] == []
        assert result["count"] == 0

    def test_hybrid_path_propagates_vector_error(self, monkeypatch):
        """默认（混合检索）路径同样要透出故障：向量库抛错不能吞成空结果。"""
        import rag.bm25_search as bm
        import rag.retriever as retriever

        def boom(*a, **k):
            raise RuntimeError("chroma down")

        monkeypatch.setattr(bm, "search_by_owner", boom)
        result = retriever._retrieve_impl("q", 1, 10, 5, use_hybrid=True, bm25_weight=0.3)

        assert result["error"] is not None
        assert result["documents"] == []

    def test_vector_store_raises_instead_of_returning_empty(self, monkeypatch):
        import rag.vector_store as vs

        def boom():
            raise RuntimeError("chroma down")

        monkeypatch.setattr(vs, "get_vector_store", boom)
        with pytest.raises(vs.VectorStoreError):
            vs.search_by_owner("q", 1, top_k=2)

    def test_collection_stats_failure_carries_error_key(self, monkeypatch):
        import rag.vector_store as vs

        def boom():
            raise RuntimeError("collection down")

        monkeypatch.setattr(vs, "_get_or_create_collection", boom)
        stats = vs.get_collection_stats(1)

        # 旧键仍在（向后兼容），新增 error 键让"查询失败"不再伪装成"知识库为空"
        assert stats["total_chunks"] == 0
        assert stats["unique_files"] == 0
        assert stats["files"] == []
        assert stats["error"] is not None
        assert "失败" in stats["error"]


# ============================================================
# 5. 分块重叠语义（选 B：语义分块不重叠，overlap 只作用于定长兜底）
# ============================================================


class TestSplitterOverlapSemantics:
    def test_structural_split_has_no_overlap(self):
        """标题分块：每块以自己标题开头，不含上一块内容（没有重叠）。"""
        from rag.text_splitter import SemanticTextSplitter

        # 每块内容 > min_chunk_length(20)，否则会被短块过滤掉
        jia = "甲" * 30
        yi = "乙" * 30
        bing = "丙" * 30
        text = f"# 第一节\n{jia}\n# 第二节\n{yi}\n# 第三节\n{bing}\n"
        # 故意给一个很大的 overlap：结构分块路径应当完全无视它
        splitter = SemanticTextSplitter(chunk_size=200, chunk_overlap=200)
        chunks = splitter.split_text(text)

        assert len(chunks) == 3
        assert chunks[0].startswith("# 第一节")
        assert jia in chunks[0]
        # 第二块不含第一块的内容 → 无重叠
        assert jia not in chunks[1]
        assert chunks[1].startswith("# 第二节")
        assert yi in chunks[1]

    def test_overlap_value_does_not_affect_structural_split(self):
        """chunk_overlap 取 0 还是 200，结构分块结果完全一致（证明它不在这条路径生效）。"""
        from rag.text_splitter import SemanticTextSplitter

        text = "# 一\n" + "甲" * 30 + "\n# 二\n" + "乙" * 30 + "\n# 三\n" + "丙" * 30 + "\n"
        no_overlap = SemanticTextSplitter(chunk_size=200, chunk_overlap=0).split_text(text)
        big_overlap = SemanticTextSplitter(chunk_size=200, chunk_overlap=200).split_text(text)
        assert no_overlap == big_overlap
        assert len(no_overlap) == 3

    def test_fallback_path_actually_uses_overlap(self):
        """无结构信息 → 走定长兜底，此时 chunk_overlap 真正产生相邻块重叠。"""
        from rag.text_splitter import SemanticTextSplitter

        # 单行长串：无标题 / 无段落 / 无编号，只能走定长兜底
        text = "abcdefghij" * 30  # 300 字符
        splitter = SemanticTextSplitter(chunk_size=50, chunk_overlap=20)
        chunks = splitter.split_text(text)

        assert len(chunks) > 1
        # 相邻块共享 20 个字符的重叠
        assert chunks[0][-20:] == chunks[1][:20]

    def test_deduplicate_has_no_dead_threshold_param(self):
        """死参数 threshold 已删除；精确去重行为不变。"""
        from rag.text_splitter import deduplicate_chunks

        assert "threshold" not in inspect.signature(deduplicate_chunks).parameters
        # 精确去重：按 strip 后的内容判重，保留首次出现的原始串
        assert deduplicate_chunks(["a", "a", "b"]) == ["a", "b"]
        assert deduplicate_chunks(["x", " x"]) == ["x"]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
