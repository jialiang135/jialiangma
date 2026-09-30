"""
评测模块"能证明优化有效"改造的测试
====================================

覆盖这次改的五件事，**全部离线**（不调 LLM / Embedding / 向量库 / ragas，
RAGAS 判定一律用假实现替换）。参考 ``tests/test_evidence.py`` 的写法。

覆盖点:
1. 配置快照 —— 确实写进报告、能被读出来;覆盖字段区分"生效/仅记录"。
2. ``retrieve_overrides`` —— 经 contextvar 注入检索参数，**不污染全局 settings**。
3. 检索为空的题 —— 被标记且**不参与指标聚合**（不再喂占位串）。
4. RAGAS 失败 —— 状态是 failed/partial，**不再是 done**。
5. ``_looks_like_refusal`` —— "先拒后答"判为**非**拒答。
6. compare —— 标出配置差异字段、指标 delta;样本数不同时明确"不可直接比较"。
"""

import json

import pytest

from tests.conftest import run_async

ALL_METRICS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]


# ============================================================
# 1. _looks_like_refusal：先拒后答不再被误判
# ============================================================


class TestLooksLikeRefusal:
    def test_pure_refusal_is_refusal(self):
        from core.eval_runner import _looks_like_refusal

        assert _looks_like_refusal("我的知识库中没有这方面的信息。") is True
        assert _looks_like_refusal("知识库中未找到相关内容。") is True

    def test_verbose_refusal_still_refusal(self):
        """啰嗦但没给实质内容的拒绝，仍判为拒答（召回）。"""
        from core.eval_runner import _looks_like_refusal

        assert (
            _looks_like_refusal("抱歉，知识库中未找到相关内容，建议您补充上传相关资料后再试。")
            is True
        )

    def test_refuse_then_answer_is_not_refusal(self):
        """核心回归:先拒后答（编造内容）必须判为**非**拒答。"""
        from core.eval_runner import _looks_like_refusal

        answer = "知识库中没有这方面的信息。不过据我所知，他曾在字节跳动做算法工程师。"
        assert _looks_like_refusal(answer) is False

        answer2 = "我的知识库中没有这方面的信息，但根据我的经验，这个项目用的是 Python。"
        assert _looks_like_refusal(answer2) is False

    def test_no_marker_or_empty_is_not_refusal(self):
        from core.eval_runner import _looks_like_refusal

        assert _looks_like_refusal("他毕业于清华大学计算机系。") is False
        assert _looks_like_refusal("") is False
        assert _looks_like_refusal("   ") is False


# ============================================================
# 2. retrieve_overrides：contextvar 注入，不碰全局 settings
# ============================================================


class TestRetrieveOverrides:
    def test_wrapper_injects_overrides_into_kwargs(self):
        from core.eval_runner import _RETRIEVE_OVERRIDES, _make_override_aware

        captured: dict = {}

        def base(query, owner_id, top_k_rerank=5, use_hybrid=None, **kwargs):
            captured.update(
                {"query": query, "owner_id": owner_id, "use_hybrid": use_hybrid, **kwargs}
            )
            captured["top_k_rerank"] = top_k_rerank
            return "ok"

        wrapped = _make_override_aware(base)
        # 无覆盖时透传（agent 里写死的 top_k_rerank=5 保持）
        assert wrapped("q", 1, top_k_rerank=5, use_hybrid=None) == "ok"
        assert captured["top_k_rerank"] == 5
        assert captured["use_hybrid"] is None

        # 有覆盖时覆盖优先于调用方显式传入的值
        token = _RETRIEVE_OVERRIDES.set({"top_k_rerank": 10, "use_hybrid_search": False})
        try:
            wrapped("q", 1, top_k_rerank=5, use_hybrid=None)
        finally:
            _RETRIEVE_OVERRIDES.reset(token)
        assert captured["top_k_rerank"] == 10
        assert captured["use_hybrid"] is False

    def test_override_does_not_mutate_global_settings(self):
        """最要紧的一条:覆盖只在上下文里，全局 settings 一字未动。"""
        from config.settings import settings
        from core.eval_runner import _RETRIEVE_OVERRIDES

        before_hybrid = settings.use_hybrid_search
        before_topk = settings.chunk_size

        token = _RETRIEVE_OVERRIDES.set(
            {"use_hybrid_search": not before_hybrid, "chunk_size": before_topk + 999}
        )
        try:
            assert settings.use_hybrid_search == before_hybrid  # 未被污染
            assert settings.chunk_size == before_topk
        finally:
            _RETRIEVE_OVERRIDES.reset(token)

        assert settings.use_hybrid_search == before_hybrid

    def test_install_hook_rebinds_agent_module_names(self, monkeypatch):
        """agent 里 `from rag.retriever import retrieve` 的名字也必须被替换。"""
        import agent.chat_agent as chat_agent
        import agent.tools as tools
        import rag.retriever as retriever
        from core import eval_runner

        def fake_retrieve(*args, **kwargs):
            return {"documents": []}

        monkeypatch.setattr(retriever, "retrieve", fake_retrieve)
        monkeypatch.setattr(chat_agent, "retrieve", fake_retrieve)
        monkeypatch.setattr(tools, "retrieve", fake_retrieve)

        eval_runner._install_retrieve_override_hook()

        assert getattr(retriever.retrieve, "_eval_override_aware", False) is True
        assert chat_agent.retrieve is retriever.retrieve
        assert tools.retrieve is retriever.retrieve

    def test_validate_rejects_unknown_keys(self):
        from core.eval_runner import validate_retrieve_overrides

        assert validate_retrieve_overrides({"top_k_rerank": 10}) == {"top_k_rerank": 10}
        with pytest.raises(ValueError):
            validate_retrieve_overrides({"no_such_field": 1})


# ============================================================
# 3. 配置快照
# ============================================================


class TestConfigSnapshot:
    def test_marks_applied_vs_ignored(self):
        from config.settings import settings
        from core.eval_runner import build_config_snapshot

        snap = build_config_snapshot(
            "auto_eval",
            ["faithfulness"],
            5,
            {"top_k_rerank": 10, "use_hybrid_search": False, "chunk_size": 2000},
        )
        # 生效的覆盖
        assert snap["top_k_rerank"] == 10
        assert snap["use_hybrid_search"] is False
        assert snap["applied_overrides"] == {"top_k_rerank": 10, "use_hybrid_search": False}
        # 只记录不生效的（改 chunk_size 要重建知识库）
        assert snap["ignored_overrides"] == {"chunk_size": 2000}
        assert snap["chunk_size"] == settings.chunk_size  # 生效值**未**变
        # 基线字段齐备
        for key in ("top_k_search", "bm25_weight", "retrieval_min_score", "model", "testset"):
            assert key in snap

    def test_snapshot_persisted_and_readable(self, monkeypatch):
        """跑一次评测后，快照确实落库、能通过 get_eval_report 读出来。"""
        report = _run_eval(monkeypatch, overrides={"top_k_rerank": 10})
        assert report["config_json"] is not None
        config = json.loads(report["config_json"])
        assert config["top_k_rerank"] == 10
        assert config["applied_overrides"]["top_k_rerank"] == 10
        assert config["testset"] == "auto_eval"


# ============================================================
# 4/5. 检索为空被排除 + RAGAS 失败状态
# ============================================================


class TestRetrievalFailedAndStatus:
    def test_retrieval_failed_question_marked_and_excluded(self, monkeypatch):
        """检索为空的题被标记，且不进 RAGAS 样本、不参与聚合。"""
        captures = _patch_ragas(
            monkeypatch, scores={"faithfulness": [0.9], "answer_relevancy": [0.8]}
        )
        questions = [
            {"id": "q1", "category": "项目", "question": "做过什么项目", "expected_answer": "RAG"},
            {"id": "q2", "category": "项目", "question": "空检索题", "expected_answer": "x"},
        ]

        def fake_answer(question, owner_id):
            if question == "空检索题":
                return "知识库中没有这方面的信息。", [], []  # 检索为空
            return "他做过 RAG 项目。", ["RAG 项目上下文"], ["步骤"]

        report = _run_eval(
            monkeypatch,
            questions=questions,
            answer_fn=fake_answer,
            scores_patch=captures,
            metrics=["faithfulness", "answer_relevancy"],
        )

        results = json.loads(report["results_json"])
        by_id = {q["id"]: q for q in results}
        assert by_id["q2"]["retrieval_failed"] is True
        assert by_id["q1"]["retrieval_failed"] is False
        # 只有 1 题进了 RAGAS 判定
        assert captures["n_samples"] == 1
        # 报告里能看出排除了几题
        assert report["poor_retrieval_count"] == 1
        # 有排除 → 状态至少是 partial（不是 done）
        assert report["status"] == "partial"
        assert "排除" in (report["error"] or "")

    def test_ragas_failure_is_not_done(self, monkeypatch):
        """RAGAS 抛异常时状态不能是 done。"""
        capture = _patch_ragas(monkeypatch, raise_error=True)
        report = _run_eval(monkeypatch, metrics=["faithfulness"], scores_patch=capture)
        assert report["status"] in ("failed", "partial")
        assert report["status"] != "done"
        assert report["error"]  # 带原因

    def test_all_success_is_done(self, monkeypatch):
        _patch_ragas(
            monkeypatch,
            scores={
                "faithfulness": [0.9, 0.9],
                "answer_relevancy": [0.9, 0.9],
                "context_precision": [0.9, 0.9],
                "context_recall": [0.9, 0.9],
            },
        )
        report = _run_eval(monkeypatch, metrics=list(ALL_METRICS))
        assert report["status"] == "done"
        assert report["poor_retrieval_count"] == 0


# ============================================================
# 6. compare 接口
# ============================================================


def _report_dict(rid, status, count, config, metrics, results, testset="auto_eval"):
    return {
        "id": rid,
        "status": status,
        "testset_name": testset,
        "total_questions": count,
        "created_at": "2026-09-29T00:00:00",
        "config_json": json.dumps(config, ensure_ascii=False),
        "metrics_json": json.dumps(metrics, ensure_ascii=False),
        "results_json": json.dumps(results, ensure_ascii=False),
    }


class TestCompare:
    def test_marks_config_diff_and_metric_delta(self):
        from core.eval_runner import build_report_comparison

        cfg_a = {"top_k_rerank": 5, "use_hybrid_search": True, "testset": "auto_eval"}
        cfg_b = {"top_k_rerank": 10, "use_hybrid_search": False, "testset": "auto_eval"}
        results = [
            {
                "id": "q1",
                "category": "项目",
                "question": "q1",
                "refused": False,
                "retrieval_failed": False,
                "faithfulness": 0.9,
                "answer": "a",
            },
        ]
        ra = _report_dict(1, "done", 1, cfg_a, {"faithfulness": 0.7}, results)
        rb = _report_dict(2, "done", 1, cfg_b, {"faithfulness": 0.9}, results)

        out = build_report_comparison(ra, rb)

        assert out["comparable"] is True
        assert out["config_diff"]["top_k_rerank"] == {"a": 5, "b": 10}
        assert out["config_diff"]["use_hybrid_search"] == {"a": True, "b": False}
        assert "testset" not in out["config_diff"]  # 相同字段不出现
        assert out["metrics"]["faithfulness"] == {"a": 0.7, "b": 0.9, "delta": 0.2}

    def test_sample_count_mismatch_flags_not_comparable(self):
        from core.eval_runner import build_report_comparison

        cfg = {"testset": "auto_eval"}
        ra = _report_dict(1, "done", 2, cfg, {"faithfulness": 0.7}, [])
        rb = _report_dict(2, "done", 5, cfg, {"faithfulness": 0.9}, [])

        out = build_report_comparison(ra, rb)

        assert out["comparable"] is False
        assert any("样本数不同" in n and "不可直接比较" in n for n in out["comparability_notes"])
        assert out["a"]["sample_count"] == 2
        assert out["b"]["sample_count"] == 5

    def test_per_question_winner_lists(self):
        from core.eval_runner import build_report_comparison

        cfg = {"testset": "auto_eval"}
        # q1: A 对（幻觉题且拒答）B 错; q2: B 对 A 错
        qa = [
            {
                "id": "q1",
                "category": "幻觉检测",
                "question": "q1",
                "refused": True,
                "retrieval_failed": False,
                "answer": "知识库中没有",
            },
            {
                "id": "q2",
                "category": "项目",
                "question": "q2",
                "refused": False,
                "retrieval_failed": False,
                "faithfulness": 0.5,
                "answer": "编的",
            },
        ]
        qb = [
            {
                "id": "q1",
                "category": "幻觉检测",
                "question": "q1",
                "refused": False,
                "retrieval_failed": False,
                "answer": "他其实在字节",
            },
            {
                "id": "q2",
                "category": "项目",
                "question": "q2",
                "refused": False,
                "retrieval_failed": False,
                "faithfulness": 0.95,
                "answer": "有据",
            },
        ]
        ra = _report_dict(1, "done", 2, cfg, {}, qa)
        rb = _report_dict(2, "done", 2, cfg, {}, qb)

        out = build_report_comparison(ra, rb)
        pq = out["per_question"]
        assert [e["id"] for e in pq["a_correct_b_wrong"]] == ["q1"]
        assert [e["id"] for e in pq["b_correct_a_wrong"]] == ["q2"]
        assert pq["common_count"] == 2

    def test_compare_route_returns_payload(self, monkeypatch):
        """走一遍路由函数（不启 TestClient），确认取报告 + 组装链路通。"""
        from api.routes.eval_routes import compare_reports
        from core.db.eval_reports import create_eval_report, update_eval_report

        run_async(init_db())

        rid_a = run_async(create_eval_report(1, "auto_eval", 0))
        run_async(
            update_eval_report(
                rid_a,
                status="done",
                total_questions=1,
                config_json=json.dumps({"top_k_rerank": 5, "testset": "auto_eval"}),
                metrics_json=json.dumps({"faithfulness": 0.6}),
                results_json=json.dumps([]),
            )
        )
        rid_b = run_async(create_eval_report(1, "auto_eval", 0))
        run_async(
            update_eval_report(
                rid_b,
                status="done",
                total_questions=1,
                config_json=json.dumps({"top_k_rerank": 10, "testset": "auto_eval"}),
                metrics_json=json.dumps({"faithfulness": 0.85}),
                results_json=json.dumps([]),
            )
        )

        payload = run_async(
            compare_reports(a=rid_a, b=rid_b, user={"owner_id": 1, "role": "admin"})
        )
        assert payload["success"] is True
        assert payload["config_diff"]["top_k_rerank"] == {"a": 5, "b": 10}
        assert payload["metrics"]["faithfulness"]["delta"] == pytest.approx(0.25)


# ============================================================
# 测试脚手架：假 RAGAS + 假问答
# ============================================================


async def init_db():
    from core.db.engine import init_database

    await init_database()


class _FakeDF:
    def __init__(self, data):
        self._data = data

    def to_dict(self, orient="list"):
        return self._data


class _FakeResult:
    def __init__(self, data):
        self._data = data

    def to_pandas(self):
        return _FakeDF(self._data)


class _FakeDataset:
    @classmethod
    def from_list(cls, items):
        return list(items)


def _patch_ragas(monkeypatch, scores=None, raise_error=False) -> dict:
    """
    用假实现替换 ragas（不导入真 ragas、不调 LLM）。

    返回一个 capture dict：``n_samples`` 记录真正送进判定的样本数。
    """
    from core import eval_runner

    capture: dict = {"n_samples": None}

    def fake_evaluate(dataset=None, metrics=None, llm=None, embeddings=None):
        if raise_error:
            raise RuntimeError("ragas boom")
        capture["n_samples"] = len(dataset or [])
        return _FakeResult(scores or {})

    fake = {
        "EvaluationDataset": _FakeDataset,
        "evaluate": fake_evaluate,
        "LangchainLLMWrapper": lambda x: x,
        "LangchainEmbeddingsWrapper": lambda x: x,
        "metrics": {m: object() for m in ALL_METRICS},
    }
    monkeypatch.setattr(eval_runner, "_load_ragas", lambda: fake)
    # 判定用的 LLM / Embedding 构造也换掉，避免真实客户端初始化
    from config.context import get_context

    class _StubChat:
        def chat_model(self, **_kw):
            return object()

        async def aclose(self):
            return None

    class _StubEmbed:
        def embeddings(self):
            return object()

        def rerank(self, _query, _documents, _top_n=5):
            return []

    monkeypatch.setattr(get_context(), "chat", _StubChat())
    monkeypatch.setattr(get_context(), "embed", _StubEmbed())
    return capture


def _run_eval(
    monkeypatch,
    questions=None,
    answer_fn=None,
    metrics=None,
    limit=None,
    overrides=None,
    scores_patch=None,
):
    """
    离线跑一次 ``run_eval_task`` 并返回落库后的报告 dict。

    把 ``load_testset`` / ``_answer_one_sync`` / ``run_async_from_thread`` 换成
    本地实现;RAGAS 由调用方先 ``_patch_ragas`` 或此处默认补一个成功的假实现。
    """
    from core import eval_runner
    from core.db.eval_reports import create_eval_report, get_eval_report

    run_async(init_db())

    if questions is None:
        questions = [
            {"id": "q1", "category": "项目", "question": "做过什么项目", "expected_answer": "RAG"},
            {"id": "q2", "category": "技能", "question": "会什么技术", "expected_answer": "Python"},
        ]
    if answer_fn is None:

        def answer_fn(question, owner_id):
            return "他做过 RAG 项目。", ["上下文片段"], ["步骤"]

    if scores_patch is None:
        n = len(questions)
        _patch_ragas(
            monkeypatch,
            scores={m: [0.9] * n for m in ALL_METRICS},
        )

    monkeypatch.setattr(eval_runner, "load_testset", lambda name: questions)
    monkeypatch.setattr(eval_runner, "_answer_one_sync", answer_fn)
    # run_async_from_thread 依赖主事件循环;测试里直接 asyncio.run 跑掉协程
    monkeypatch.setattr(
        eval_runner, "run_async_from_thread", lambda coro, timeout=30.0: run_async(coro)
    )

    report_id = run_async(create_eval_report(1, "auto_eval", 0))
    eval_runner.run_eval_task(
        report_id,
        1,
        "auto_eval",
        metrics or list(ALL_METRICS),
        limit,
        overrides,
    )
    return run_async(get_eval_report(report_id))


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
