"""
scripts/eval_retrieval.py 的纯逻辑单测（离线，不联网、不读配置、不调 API）。

用 importlib 按文件路径加载脚本 —— 脚本顶层只 import 标准库，重依赖
（config.settings / rag.retriever / core.eval_runner）全部在函数内延迟导入，
因此加载它不会触发任何副作用。
"""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "eval_retrieval.py"
_spec = importlib.util.spec_from_file_location("eval_retrieval", _SCRIPT_PATH)
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)


# ------------------------------------------------------------
# first_hit_rank / hit@k 边界
# ------------------------------------------------------------


def test_first_hit_rank_finds_position():
    assert er.first_hit_rank(["a", "b", "c"], ["c"]) == 3
    assert er.first_hit_rank(["a", "b", "c"], ["a"]) == 1
    # 多个期望文档时取最先命中的那个
    assert er.first_hit_rank(["a", "b", "c"], ["c", "b"]) == 2


def test_first_hit_rank_empty_expected_returns_none():
    # 无答案题：没有可命中目标
    assert er.first_hit_rank(["a", "b"], []) is None


def test_first_hit_rank_empty_sources_returns_none():
    assert er.first_hit_rank([], ["a"]) is None


def test_first_hit_rank_no_match_returns_none():
    assert er.first_hit_rank(["a", "b"], ["z"]) is None


@pytest.mark.parametrize("rank", [1, 2, 3, 4, 5])
def test_hit_at_k_inclusive_boundary(rank):
    # 期望文档正好排在第 k 名，应算命中（含边界）
    sources = [f"d{i}" for i in range(1, rank)] + ["target"]
    assert er.hit_at_k(sources, ["target"], rank) is True


@pytest.mark.parametrize("k", [1, 3, 5])
def test_hit_at_k_just_outside_boundary(k):
    # 期望文档排在第 k+1 名，超出 top-k，应算未命中
    sources = [f"d{i}" for i in range(1, k + 1)] + ["target"]
    assert er.hit_at_k(sources, ["target"], k) is False


def test_hit_at_k_empty_expected_is_false():
    # 无答答题没有目标，hit 恒为 False（不参与聚合，但要保证不崩）
    assert er.hit_at_k(["a"], [], 1) is False


def test_hit_at_k_empty_sources_is_false():
    assert er.hit_at_k([], ["a"], 5) is False


# ------------------------------------------------------------
# MRR
# ------------------------------------------------------------


def test_reciprocal_rank_values():
    assert er.reciprocal_rank(["a"], ["a"]) == 1.0
    assert er.reciprocal_rank(["a", "b"], ["b"]) == 0.5
    assert er.reciprocal_rank(["a", "b", "c", "d"], ["d"]) == 0.25
    assert er.reciprocal_rank(["a", "b"], ["z"]) == 0.0


def test_reciprocal_rank_prefers_earliest_hit():
    # 命中多个期望文档时，取排名更靠前的那个
    assert er.reciprocal_rank(["a", "b", "c"], ["c", "a"]) == 1.0


def test_reciprocal_rank_empty_expected_is_zero():
    assert er.reciprocal_rank(["a"], []) == 0.0


# ------------------------------------------------------------
# score_question 分组
# ------------------------------------------------------------


def test_score_question_answerable():
    rec = er.score_question(["a", "b", "c"], ["c"], ks=(1, 3, 5))
    assert rec["is_no_answer"] is False
    assert rec["is_error"] is False
    assert rec["hit@1"] is False
    assert rec["hit@3"] is True
    assert rec["hit@5"] is True
    assert rec["rank"] == 3
    assert rec["rr"] == pytest.approx(1 / 3)


def test_score_question_no_answer():
    rec = er.score_question(["a"], [], ks=(1, 3, 5), top_score=0.12)
    assert rec["is_no_answer"] is True
    assert rec["returned"] == 1
    assert rec["top_score"] == 0.12
    assert "rr" not in rec  # 无答案题不产生 hit/MRR


def test_score_question_error_takes_precedence():
    # error 非空时既不算命中也不算「确实没有」，单独成组
    rec = er.score_question(["a"], ["a"], ks=(1,), error="向量库挂了")
    assert rec["is_error"] is True
    assert rec["is_no_answer"] is False
    assert "rr" not in rec


# ------------------------------------------------------------
# aggregate（含无答案题与故障题分组）
# ------------------------------------------------------------


def _sample_records():
    return [
        er.score_question(["a"], ["a"], ks=(1, 3, 5)),  # rank1
        er.score_question(["b", "c"], ["c"], ks=(1, 3, 5)),  # rank2
        er.score_question(["x"], ["y"], ks=(1, 3, 5)),  # miss
        er.score_question([], [], ks=(1, 3, 5)),  # 无答案且返回 0 条
        er.score_question(["z"], [], ks=(1, 3, 5), top_score=0.2),  # 无答案但返回 1 条
        er.score_question(["a"], ["a"], ks=(1, 3, 5), error="boom"),  # 故障
    ]


def test_aggregate_metrics():
    m = er.aggregate(_sample_records(), ks=(1, 3, 5))
    assert m["total"] == 6
    assert m["answerable"] == 3
    assert m["no_answer"] == 2
    assert m["errors"] == 1
    # hit@1 = 1/3，hit@3 = 2/3，hit@5 = 2/3
    assert m["hit@1"] == pytest.approx(round(1 / 3, 4))
    assert m["hit@3"] == pytest.approx(round(2 / 3, 4))
    assert m["hit@5"] == pytest.approx(round(2 / 3, 4))
    # MRR = (1 + 0.5 + 0) / 3
    assert m["mrr"] == pytest.approx(0.5)
    # 无答案题：2 条里 1 条返回 0 条
    assert m["no_answer_empty_rate"] == pytest.approx(0.5)
    assert m["no_answer_mean_top_score"] == pytest.approx(0.2)
    assert m["error_messages"] == ["boom"]


def test_aggregate_empty_input_is_zero_safe():
    m = er.aggregate([], ks=(1, 3, 5))
    assert m["total"] == 0
    assert m["hit@1"] == 0.0
    assert m["mrr"] == 0.0
    assert m["no_answer_empty_rate"] is None
    assert m["no_answer_mean_top_score"] is None


def test_aggregate_only_no_answer_no_top_score():
    m = er.aggregate([er.score_question([], [], ks=(1,))], ks=(1,))
    assert m["answerable"] == 0
    assert m["no_answer_empty_rate"] == 1.0
    assert m["no_answer_mean_top_score"] is None


# ------------------------------------------------------------
# A/B delta
# ------------------------------------------------------------


def test_metric_delta_computes_b_minus_a():
    a = {"hit@1": 0.4, "hit@3": 0.7, "hit@5": 0.9, "mrr": 0.55}
    b = {"hit@1": 0.6, "hit@3": 0.8, "hit@5": 0.85, "mrr": 0.70}
    d = er.metric_delta(a, b)
    assert d["hit@1"] == pytest.approx(0.2)
    assert d["hit@3"] == pytest.approx(0.1)
    assert d["hit@5"] == pytest.approx(-0.05)
    assert d["mrr"] == pytest.approx(0.15)


def test_metric_delta_skips_missing_keys():
    assert er.metric_delta({"hit@1": 0.1}, {"hit@1": 0.2}) == {"hit@1": pytest.approx(0.1)}
    assert er.metric_delta({}, {"hit@1": 0.2}) == {}


# ------------------------------------------------------------
# CLI 解析
# ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("use_hybrid_search=true", {"use_hybrid_search": True}),
        ("use_hybrid_search=false", {"use_hybrid_search": False}),
        ("top_k_search=20", {"top_k_search": 20}),
        ("bm25_weight=0.5", {"bm25_weight": 0.5}),
        ("a=1,b=true", {"a": 1, "b": True}),
        ("", {}),
        (None, {}),
    ],
)
def test_parse_overrides(raw, expected):
    assert er.parse_overrides(raw) == expected


def test_parse_overrides_rejects_bad_format():
    with pytest.raises(ValueError):
        er.parse_overrides("use_hybrid_search")


def test_parse_ks():
    assert er.parse_ks("1,3,5") == (1, 3, 5)
    assert er.parse_ks("5,1") == (1, 5)
    with pytest.raises(ValueError):
        er.parse_ks("0")


# ------------------------------------------------------------
# 报告渲染（冒烟：不崩、包含关键指标行）
# ------------------------------------------------------------


def test_render_report_single_and_ab_smoke():
    run_a = {
        "metrics": {
            "answerable": 3,
            "no_answer": 1,
            "errors": 0,
            "hit@1": 0.4,
            "hit@3": 0.7,
            "hit@5": 0.9,
            "mrr": 0.55,
            "no_answer_empty_rate": 1.0,
            "no_answer_mean_top_score": None,
            "mean_latency_s": 1.2,
        },
        "per_question": [],
    }
    run_b = {
        "metrics": {
            "answerable": 3,
            "no_answer": 1,
            "errors": 0,
            "hit@1": 0.6,
            "hit@3": 0.8,
            "hit@5": 0.85,
            "mrr": 0.70,
            "no_answer_empty_rate": 1.0,
            "no_answer_mean_top_score": None,
            "mean_latency_s": 0.9,
        },
        "per_question": [],
    }
    single = er.render_report("单配置", [("base", run_a)], ks=(1, 3, 5))
    assert "hit@1" in single and "mrr" in single
    ab = er.render_report("A/B", [("A", run_a), ("B", run_b)], ks=(1, 3, 5))
    assert "Δ(B-A)" in ab
    assert "hit@1" in ab
