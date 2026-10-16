"""Evaluation routes validate inputs and preserve report ownership on failures."""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tests.conftest import run_async

ADMIN = {"owner_id": 1, "role": "admin"}
MEMBER = {"owner_id": 7, "role": "user"}


def test_testsets_endpoint_returns_sorted_allowed_metrics(monkeypatch):
    import api.routes.eval_routes as routes

    monkeypatch.setattr(routes, "list_testsets", lambda: [{"name": "set-a"}])
    result = run_async(routes.get_testsets(MEMBER))
    assert result == {
        "success": True,
        "testsets": [{"name": "set-a"}],
        "metrics": sorted(routes.ALLOWED_METRICS),
    }


def test_eval_request_schema_bounds_sample_count():
    from api.routes.eval_routes import EvalRunRequest

    assert EvalRunRequest().testset == "auto_eval"
    assert EvalRunRequest(sample_limit=1).sample_limit == 1
    assert EvalRunRequest(sample_limit=100).sample_limit == 100
    for sample_limit in (0, 101):
        with pytest.raises(ValidationError):
            EvalRunRequest(sample_limit=sample_limit)


def test_run_evaluation_rejects_empty_metrics_and_invalid_overrides(monkeypatch):
    import api.routes.eval_routes as routes

    with pytest.raises(HTTPException) as no_metrics:
        run_async(routes.run_evaluation(routes.EvalRunRequest(metrics=["unknown"]), ADMIN))
    assert no_metrics.value.status_code == 400

    monkeypatch.setattr(
        routes,
        "validate_retrieve_overrides",
        lambda _overrides: (_ for _ in ()).throw(ValueError("unsupported override")),
    )
    with pytest.raises(HTTPException) as invalid_override:
        run_async(
            routes.run_evaluation(
                routes.EvalRunRequest(metrics=["faithfulness"], retrieve_overrides={"bad": 1}),
                ADMIN,
            )
        )
    assert invalid_override.value.status_code == 400
    assert invalid_override.value.detail == "unsupported override"


def test_run_evaluation_enqueues_filtered_metrics_and_returns_snapshot(monkeypatch):
    import api.routes.eval_routes as routes

    calls = []

    async def create(owner_id, testset, _progress):
        calls.append(("create", owner_id, testset))
        return 42

    class Queue:
        def enqueue(self, *args):
            calls.append(("enqueue", args))

    def validate(overrides):
        assert overrides == {"top_k_rerank": 4}
        return {"top_k_rerank": 4}

    def snapshot(testset, metrics, limit, overrides):
        calls.append(("snapshot", testset, metrics, limit, overrides))
        return {"testset": testset, "metric_count": len(metrics)}

    monkeypatch.setattr(routes, "create_eval_report", create)
    monkeypatch.setattr(routes, "async_queue", Queue())
    monkeypatch.setattr(routes, "validate_retrieve_overrides", validate)
    monkeypatch.setattr(routes, "build_config_snapshot", snapshot)
    body = routes.EvalRunRequest(
        testset="custom",
        metrics=["faithfulness", "bogus", "context_recall"],
        sample_limit=8,
        retrieve_overrides={"top_k_rerank": 4},
    )

    result = run_async(routes.run_evaluation(body, ADMIN))
    assert result["success"] is True
    assert result["data"] == {
        "report_id": 42,
        "testset": "custom",
        "metrics": ["faithfulness", "context_recall"],
        "sample_limit": 8,
        "retrieve_overrides": {"top_k_rerank": 4},
        "config_snapshot": {"testset": "custom", "metric_count": 2},
    }
    assert calls[0] == ("create", 1, "custom")
    enqueue = calls[1][1]
    assert enqueue[0] is routes.run_eval_task
    assert enqueue[1:] == (42, 1, "custom", ["faithfulness", "context_recall"], 8, {"top_k_rerank": 4})


def test_queue_submission_failure_marks_report_failed_and_returns_503(monkeypatch):
    import api.routes.eval_routes as routes

    updates = []

    async def create(_owner_id, _testset, _progress):
        return 55

    async def update(report_id, **kwargs):
        updates.append((report_id, kwargs))

    class Queue:
        def enqueue(self, *_args):
            raise RuntimeError("queue offline " + "x" * 250)

    monkeypatch.setattr(routes, "create_eval_report", create)
    monkeypatch.setattr(routes, "update_eval_report", update)
    monkeypatch.setattr(routes, "async_queue", Queue())
    with pytest.raises(HTTPException) as error:
        run_async(routes.run_evaluation(routes.EvalRunRequest(metrics=["faithfulness"]), ADMIN))
    assert error.value.status_code == 503
    assert len(error.value.detail) <= len("评测任务提交失败: ") + 200
    assert updates[0][0] == 55
    assert updates[0][1]["status"] == "failed"
    assert updates[0][1]["error"].startswith("任务提交失败: queue offline")


def test_reports_and_detail_scope_admin_to_all_and_member_to_owner(monkeypatch):
    import api.routes.eval_routes as routes

    seen = []

    async def list_reports(owner_id, *, limit):
        seen.append((owner_id, limit))
        return [{"id": 1}]

    async def get_report(report_id, owner_id):
        seen.append((report_id, owner_id))
        if report_id == 2 and owner_id == 7:
            return {"id": report_id, "owner_id": owner_id}
        return None

    monkeypatch.setattr(routes, "list_eval_reports", list_reports)
    monkeypatch.setattr(routes, "get_eval_report", get_report)

    admin_reports = run_async(routes.get_reports(limit=5, user=ADMIN))
    member_reports = run_async(routes.get_reports(limit=4, user=MEMBER))
    assert admin_reports["reports"] == member_reports["reports"] == [{"id": 1}]
    assert seen[:2] == [(None, 5), (7, 4)]

    detail = run_async(routes.get_report_detail(2, MEMBER))
    assert detail == {"success": True, "report": {"id": 2, "owner_id": 7}}
    with pytest.raises(HTTPException) as hidden:
        run_async(routes.get_report_detail(2, {"owner_id": 8, "role": "user"}))
    assert hidden.value.status_code == 404
    assert seen[-2:] == [(2, 7), (2, 8)]


def test_compare_reports_checks_access_to_both_and_returns_comparison(monkeypatch):
    import api.routes.eval_routes as routes

    async def get_report(report_id, owner_id):
        if report_id == 1 or (report_id == 2 and owner_id is None):
            return {"id": report_id}
        return None

    monkeypatch.setattr(routes, "get_eval_report", get_report)
    monkeypatch.setattr(
        routes,
        "build_report_comparison",
        lambda a, b: {"comparable": True, "ids": [a["id"], b["id"]]},
    )

    with pytest.raises(HTTPException) as missing_a:
        run_async(routes.compare_reports(9, 2, MEMBER))
    assert missing_a.value.status_code == 404
    with pytest.raises(HTTPException) as missing_b:
        run_async(routes.compare_reports(1, 2, MEMBER))
    assert missing_b.value.status_code == 404
    assert run_async(routes.compare_reports(1, 2, ADMIN)) == {
        "success": True,
        "comparable": True,
        "ids": [1, 2],
    }


def test_delete_report_returns_not_found_or_success(monkeypatch):
    import api.routes.eval_routes as routes

    async def delete(report_id):
        return report_id == 3

    monkeypatch.setattr(routes, "delete_eval_report", delete)
    with pytest.raises(HTTPException) as missing:
        run_async(routes.remove_report(2, ADMIN))
    assert missing.value.status_code == 404
    assert run_async(routes.remove_report(3, ADMIN)) == {
        "success": True,
        "message": "评测报告 #3 已删除",
    }
