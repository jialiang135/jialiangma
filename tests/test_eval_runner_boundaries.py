"""Offline boundary tests for evaluation testsets and metric normalization."""

import json
from types import SimpleNamespace


def test_testset_listing_skips_malformed_files_and_empty_dirs(monkeypatch, tmp_path):
    import core.eval_runner as runner

    test_dir = tmp_path / "sets"
    test_dir.mkdir()
    (test_dir / "zeta.json").write_text(
        json.dumps({"name": "Zeta Set", "questions": [{"question": "q1"}, {"question": "q2"}]}),
        encoding="utf-8",
    )
    (test_dir / "alpha.json").write_text("not json", encoding="utf-8")
    (test_dir / "empty.json").write_text(json.dumps({"questions": []}), encoding="utf-8")
    monkeypatch.setattr(
        runner,
        "settings",
        SimpleNamespace(test_data_dir=str(test_dir), resolve_path=lambda value: test_dir),
    )

    assert runner.list_testsets() == [
        {"name": "empty", "title": "empty", "count": 0},
        {"name": "zeta", "title": "Zeta Set", "count": 2},
    ]

    missing = tmp_path / "missing"
    monkeypatch.setattr(
        runner,
        "settings",
        SimpleNamespace(test_data_dir=str(missing), resolve_path=lambda value: missing),
    )
    assert runner.list_testsets() == []


def test_testset_path_accepts_suffix_alias_but_blocks_path_traversal(monkeypatch, tmp_path):
    import pytest

    import core.eval_runner as runner

    test_dir = tmp_path / "sets"
    test_dir.mkdir()
    path = test_dir / "auto_eval_testset.json"
    path.write_text(json.dumps({"questions": [{"id": "q1"}]}), encoding="utf-8")
    outside = tmp_path / "private.json"
    outside.write_text(json.dumps({"questions": [{"id": "secret"}]}), encoding="utf-8")
    monkeypatch.setattr(
        runner,
        "settings",
        SimpleNamespace(test_data_dir=str(test_dir), resolve_path=lambda value: test_dir),
    )

    assert runner._resolve_testset_path("auto_eval") == path
    assert runner._resolve_testset_path("auto_eval_testset") == path
    assert runner.load_testset("auto_eval") == [{"id": "q1"}]
    with pytest.raises(FileNotFoundError):
        runner._resolve_testset_path("../private")


def test_sample_selection_round_robins_categories_and_unlimited_returns_all():
    import core.eval_runner as runner

    questions = [
        {"id": "a1", "category": "A"},
        {"id": "a2", "category": "A"},
        {"id": "b1", "category": "B"},
        {"id": "c1", "category": "C"},
        {"id": "b2", "category": "B"},
        {"id": "other", "question": "missing category"},
    ]
    selected = runner._select_samples(questions, 4)
    assert [item["id"] for item in selected] == ["a1", "b1", "c1", "other"]

    assert runner._select_samples(questions, None) is questions
    assert runner._select_samples(questions, len(questions)) is questions
    assert runner._select_samples(questions, 0) is questions


def test_answer_one_collects_unique_tool_evidence_and_sanitizes_scores(monkeypatch):
    from langchain_core.messages import ToolMessage

    import agent.graph_workflow as workflow
    import api.sse_stream as sse
    import core.eval_runner as runner

    first_tool = ToolMessage(
        content="【片段 1】来源: tool.md | 相关度: 0.87654\n来源: bad.md | 相关度: not-a-score",
        tool_call_id="call-1",
    )
    duplicate_tool = ToolMessage(content="来源: tool.md | 相关度: 0.87654", tool_call_id="call-2")
    infinite_tool = ToolMessage(content="来源: infinite.md | 相关度: nan", tool_call_id="call-3")

    class Graph:
        async def ainvoke(self, state):
            assert state == {"question": "question", "owner": 8}
            return {
                "final_answer": "answer",
                "retrieved_docs": [
                    {"content": "initial context", "source": "s" * 250, "score": 0.123456, "page": 2},
                    {"content": "no source evidence"},
                    {"content": "", "source": "empty", "score": float("inf")},
                ],
                "messages": [
                    first_tool,
                    duplicate_tool,
                    infinite_tool,
                    ToolMessage(content="   ", tool_call_id="call-4"),
                ],
                "reasoning_log": ["最大工具调用轮次", 42],
            }

    monkeypatch.setattr(workflow, "get_agent_graph", lambda: Graph())
    monkeypatch.setattr(sse, "_initial_state", lambda question, owner, *_args: {"question": question, "owner": owner})
    answer, contexts, steps, diagnostics = runner._answer_one_sync("question", 8)

    assert answer == "answer"
    assert contexts == [
        "initial context",
        "no source evidence",
        first_tool.content,
        duplicate_tool.content,
        infinite_tool.content,
    ]
    assert steps == ["最大工具调用轮次"]
    assert diagnostics["react_overflow"] is True
    assert diagnostics["initial"] == [
        {"source": "s" * 200, "chunk_idx": None, "page": 2, "score": 0.1235},
        {"source": "empty", "chunk_idx": None, "page": None, "score": None},
    ]
    assert diagnostics["tool_sources"] == [
        {"source": "tool.md", "score": 0.8765},
        {"source": "bad.md", "score": None},
        {"source": "infinite.md", "score": None},
    ]


def test_optional_ragas_import_failure_has_an_actionable_message(monkeypatch):
    import builtins

    import pytest

    import core.eval_runner as runner

    monkeypatch.setattr(runner, "_install_ragas_compat_shim", lambda: None)
    monkeypatch.setattr(runner, "_forward_stdlib_logs_to_loguru", lambda: None)
    original_import = builtins.__import__

    def import_without_ragas(name, *args, **kwargs):
        if name == "ragas" or name.startswith("ragas."):
            raise ImportError("ragas not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_ragas)
    with pytest.raises(RuntimeError, match="评测依赖未安装") as error:
        runner._load_ragas()
    assert isinstance(error.value.__cause__, ImportError)


def test_judge_run_config_uses_configured_limits(monkeypatch):
    import core.eval_runner as runner
    from config.settings import settings

    monkeypatch.setattr(settings, "eval_judge_concurrency", 2)
    monkeypatch.setattr(settings, "eval_judge_timeout_seconds", 123)
    monkeypatch.setattr(settings, "eval_judge_max_retries", 4)
    captured = {}

    class RunConfig:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    assert runner._build_run_config({"RunConfig": RunConfig}) is not None
    assert captured == {"max_workers": 2, "timeout": 123, "max_retries": 4}


def test_metric_normalization_rejects_nan_infinity_bool_and_non_numeric_values():
    import math

    import core.eval_runner as runner

    assert runner._as_number(3) == 3.0
    assert runner._as_number(0.25) == 0.25
    assert runner._as_number(True) is None
    assert runner._as_number("0.5") is None
    assert runner._as_number(float("nan")) is None
    assert runner._as_number(float("inf")) is None
    assert runner._as_number(float("-inf")) is None
    assert math.isfinite(runner._as_number(0.5))
