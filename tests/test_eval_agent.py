"""Behavioral coverage for the evaluation agent's user-visible responses and queueing."""

import json

import pytest
from langchain_core.messages import AIMessage

from tests.conftest import run_async


def test_report_format_renders_metrics_recommendations_and_errors():
    from agent.eval_agent import _format_report

    report = {
        "id": 17,
        "testset_name": "auto_eval",
        "status": "completed",
        "progress": 100,
        "completed": 4,
        "total_questions": 4,
        "duration_seconds": 2.5,
        "metrics_json": json.dumps(
            {"faithfulness": 0.9, "custom_metric": 0.7, "_warning": "one skipped row"}
        ),
        "honesty_rate": 0.75,
        "recommendations_json": json.dumps(["补充文档", "检查召回"]),
        "error": "partial provider warning",
    }

    text = _format_report(report)
    assert "评测报告 #17" in text
    assert "忠实度" in text and "0.9" in text
    assert "custom_metric" in text
    assert "one skipped row" in text
    assert "75%" in text
    assert "1. 补充文档" in text and "2. 检查召回" in text
    assert "partial provider warning" in text


def test_report_format_ignores_invalid_json_and_empty_optional_fields():
    from agent.eval_agent import _format_report

    report = {
        "id": 18,
        "testset_name": "sample_eval",
        "status": "running",
        "progress": 20,
        "completed": 1,
        "total_questions": 5,
        "duration_seconds": 0,
        "metrics_json": "{broken",
        "honesty_rate": None,
        "recommendations_json": "{broken",
        "error": "",
    }

    text = _format_report(report)
    assert "评测报告 #18" in text
    assert "耗时" not in text
    assert "RAGAS 指标" not in text
    assert "诚实度" not in text
    assert "改进建议" not in text
    assert "错误" not in text

    report["recommendations_json"] = "[]"
    assert "改进建议" not in _format_report(report)


@pytest.mark.parametrize(
    ("query", "expected_set"),
    [
        ("请运行评测", "default-set"),
        ("运行评测 sample_eval", "sample_eval"),
        ("运行评测 ai_interview", "ai_interview"),
        ("运行评测 auto_eval", "auto_eval"),
    ],
)
def test_run_request_enqueues_selected_testset(monkeypatch, query, expected_set):
    import agent.eval_agent as eval_agent
    from core.async_queue import async_queue

    captured = {}

    async def create_report(owner_id, testset, total):
        captured["created"] = (owner_id, testset, total)
        return 71

    def enqueue(*args):
        captured["enqueued"] = args

    monkeypatch.setattr(eval_agent, "DEFAULT_TESTSET", "default-set")
    monkeypatch.setattr(eval_agent, "create_eval_report", create_report)
    monkeypatch.setattr(async_queue, "enqueue", enqueue)

    result = run_async(
        eval_agent.eval_agent_node({"owner_id": 9, "user_query": query})
    )

    assert captured["created"] == (9, expected_set, 0)
    assert captured["enqueued"][1:5] == (71, 9, expected_set, eval_agent.DEFAULT_METRICS)
    assert captured["enqueued"][5] == eval_agent.DEFAULT_SAMPLE_LIMIT
    assert "#71" in result["final_answer"]
    assert result["reasoning_log"]
    assert isinstance(result["messages"][0], AIMessage)


def test_run_request_defaults_anonymous_owner_and_reports_enqueue_failure(monkeypatch):
    import agent.eval_agent as eval_agent
    from core.async_queue import async_queue

    updated = []

    async def create_report(owner_id, testset, total):
        assert owner_id == 0
        return 72

    async def update_report(report_id, **fields):
        updated.append((report_id, fields))

    def enqueue(*_args):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(eval_agent, "create_eval_report", create_report)
    monkeypatch.setattr(eval_agent, "update_eval_report", update_report)
    monkeypatch.setattr(async_queue, "enqueue", enqueue)

    result = run_async(eval_agent.eval_agent_node({"user_query": "开始评测"}))

    assert updated == [(72, {"status": "failed", "error": "任务提交失败: queue unavailable"})]
    assert "queue unavailable" in result["final_answer"]
    assert "错误" in result["reasoning_log"][-1]


def test_report_query_handles_empty_latest_pending_and_repository_failure(monkeypatch):
    import agent.eval_agent as eval_agent

    async def no_reports(_owner_id, limit):
        assert limit == 1
        return []

    monkeypatch.setattr(eval_agent, "list_eval_reports", no_reports)
    empty = run_async(eval_agent.eval_agent_node({"owner_id": 4, "user_query": "状态"}))
    assert "还没有评测记录" in empty["final_answer"]
    assert empty["reasoning_log"] == ["📭 暂无评测记录"]

    async def pending_report(_owner_id, limit):
        return [
            {
                "id": 81,
                "testset_name": "auto_eval",
                "status": "running",
                "progress": 40,
                "completed": 2,
                "total_questions": 5,
            }
        ]

    monkeypatch.setattr(eval_agent, "list_eval_reports", pending_report)
    pending = run_async(eval_agent.eval_agent_node({"owner_id": 4, "user_query": "状态"}))
    assert "评测报告 #81" in pending["final_answer"]
    assert "仍在进行中（进度 40%）" in pending["final_answer"]
    assert "最近一次评测: #81 (running)" in pending["reasoning_log"][0]

    async def completed_report(_owner_id, limit):
        return [
            {
                "id": 82,
                "testset_name": "auto_eval",
                "status": "completed",
                "progress": 100,
                "completed": 5,
                "total_questions": 5,
            }
        ]

    monkeypatch.setattr(eval_agent, "list_eval_reports", completed_report)
    completed = run_async(eval_agent.eval_agent_node({"owner_id": 4, "user_query": "状态"}))
    assert "评测报告 #82" in completed["final_answer"]
    assert "仍在进行中" not in completed["final_answer"]

    async def repository_failure(_owner_id, limit):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(eval_agent, "list_eval_reports", repository_failure)
    failed = run_async(eval_agent.eval_agent_node({"owner_id": 4, "user_query": "状态"}))
    assert "database unavailable" in failed["final_answer"]
    assert isinstance(failed["messages"][0], AIMessage)
