"""
知识库评测 API 路由
====================

评测是**异步任务**：一次评测要对每条问题跑一遍完整问答图，再由 LLM 逐条判定，
可能耗时几分钟到几十分钟。因此 POST /run 只负责提交任务并返回 report_id，
进度与结果通过 GET /reports/{id} 轮询 —— 与知识库重建同一模式。
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from core.async_queue import async_queue
from core.auth import get_current_user, require_admin
from core.db.eval_reports import (
    create_eval_report,
    delete_eval_report,
    get_eval_report,
    list_eval_reports,
    update_eval_report,
)
from core.eval_runner import (
    DEFAULT_METRICS,
    DEFAULT_SAMPLE_LIMIT,
    build_config_snapshot,
    build_report_comparison,
    list_testsets,
    run_eval_task,
    validate_retrieve_overrides,
)

router = APIRouter(prefix="/api/eval", tags=["评测"])

# 允许请求的 RAGAS 指标（白名单，避免把任意字符串传给 ragas）
ALLOWED_METRICS = {
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
}


class EvalRunRequest(BaseModel):
    testset: str = Field(default="auto_eval", description="评测集名称")
    metrics: list[str] = Field(default_factory=lambda: list(DEFAULT_METRICS))
    sample_limit: int = Field(
        default=DEFAULT_SAMPLE_LIMIT, ge=1, le=100, description="只评测前 N 题（全量会很慢）"
    )
    retrieve_overrides: dict[str, Any] | None = Field(
        default=None,
        description=(
            "本次评测专用的检索参数覆盖（A/B 用）。例 "
            '{"use_hybrid_search": false} 或 {"top_k_rerank": 10}。'
            "只作用于本次评测，不改全局配置。可覆盖: "
            "top_k_search / top_k_rerank / use_hybrid_search / bm25_weight / "
            "use_search_cache；retrieval_min_score / chunk_size 等会被记录进"
            "快照但不生效（需重建知识库）。"
        ),
    )


@router.get("/testsets")
async def get_testsets(user: dict = Depends(get_current_user)):
    """列出可用的评测集。"""
    return {"success": True, "testsets": list_testsets(), "metrics": sorted(ALLOWED_METRICS)}


@router.post("/run")
async def run_evaluation(
    body: EvalRunRequest,
    user: dict = Depends(require_admin),
):
    """提交一次评测任务（管理员）。返回 report_id 供轮询进度。"""
    metrics = [m for m in body.metrics if m in ALLOWED_METRICS]
    if not metrics:
        raise HTTPException(status_code=400, detail="至少选择一个有效指标")

    try:
        overrides = validate_retrieve_overrides(body.retrieve_overrides)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    owner_id = user["owner_id"]
    report_id = await create_eval_report(owner_id, body.testset, 0)

    try:
        async_queue.enqueue(
            run_eval_task,
            report_id,
            owner_id,
            body.testset,
            metrics,
            body.sample_limit,
            overrides,
        )
    except Exception as e:
        await update_eval_report(report_id, status="failed", error=f"任务提交失败: {str(e)[:200]}")
        raise HTTPException(status_code=503, detail=f"评测任务提交失败: {str(e)[:200]}") from e

    # 回给调用方一份配置快照（与落库的那份同源），这样提交后立刻能看出
    # "这次用的是什么配置、哪些覆盖真生效了"，不用等评测跑完。
    config_snapshot = build_config_snapshot(body.testset, metrics, body.sample_limit, overrides)
    logger.info(
        f"[Eval] 评测任务已提交: report={report_id}, testset={body.testset}, "
        f"metrics={metrics}, limit={body.sample_limit}, overrides={overrides}"
    )
    return {
        "success": True,
        "message": "评测任务已提交，请轮询进度",
        "data": {
            "report_id": report_id,
            "testset": body.testset,
            "metrics": metrics,
            "sample_limit": body.sample_limit,
            "retrieve_overrides": overrides,
            "config_snapshot": config_snapshot,
        },
    }


@router.get("/reports")
async def get_reports(
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    """列出评测报告（管理员看全部，普通用户看自己的）。"""
    owner_id = None if user.get("role") == "admin" else user["owner_id"]
    reports = await list_eval_reports(owner_id, limit=limit)
    return {"success": True, "total": len(reports), "reports": reports}


@router.get("/reports/{report_id}")
async def get_report_detail(report_id: int, user: dict = Depends(get_current_user)):
    """获取单次评测的详情（含逐题明细）。"""
    owner_id = None if user.get("role") == "admin" else user["owner_id"]
    report = await get_eval_report(report_id, owner_id)
    if not report:
        raise HTTPException(status_code=404, detail="评测报告不存在")
    return {"success": True, "report": report}


@router.get("/compare")
async def compare_reports(
    a: int = Query(..., description="基线报告 A 的 id"),
    b: int = Query(..., description="对比报告 B 的 id"),
    user: dict = Depends(get_current_user),
):
    """
    对比两次评测（A/B）—— 用来回答"这次优化到底有没有效果"。

    返回:两次的配置快照（并标出差异字段）、各指标 A/B 值与 delta（B-A）、
    逐题差异（A 对 B 错 / B 对 A 错 / 拒答变化 / 检索变化），以及样本数。
    样本数或评测集不同时 ``comparable=False`` 并给出原因 —— 那种情况下
    指标不能直接比。
    """
    owner_id = None if user.get("role") == "admin" else user["owner_id"]
    report_a = await get_eval_report(a, owner_id)
    if not report_a:
        raise HTTPException(status_code=404, detail=f"评测报告 A(#{a}) 不存在或无权访问")
    report_b = await get_eval_report(b, owner_id)
    if not report_b:
        raise HTTPException(status_code=404, detail=f"评测报告 B(#{b}) 不存在或无权访问")
    return {"success": True, **build_report_comparison(report_a, report_b)}


@router.delete("/reports/{report_id}")
async def remove_report(report_id: int, user: dict = Depends(require_admin)):
    """删除评测报告（管理员）。"""
    deleted = await delete_eval_report(report_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="评测报告不存在")
    return {"success": True, "message": f"评测报告 #{report_id} 已删除"}
