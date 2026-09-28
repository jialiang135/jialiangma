"""
知识库评测 API 路由
====================

评测是**异步任务**：一次评测要对每条问题跑一遍完整问答图，再由 LLM 逐条判定，
可能耗时几分钟到几十分钟。因此 POST /run 只负责提交任务并返回 report_id，
进度与结果通过 GET /reports/{id} 轮询 —— 与知识库重建同一模式。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from core.async_queue import async_queue
from core.auth import get_current_user, require_admin
from core.database import (
    create_eval_report,
    delete_eval_report,
    get_eval_report,
    list_eval_reports,
    update_eval_report,
)
from core.eval_runner import DEFAULT_METRICS, DEFAULT_SAMPLE_LIMIT, list_testsets, run_eval_task

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
        )
    except Exception as e:
        await update_eval_report(report_id, status="failed", error=f"任务提交失败: {str(e)[:200]}")
        raise HTTPException(status_code=503, detail=f"评测任务提交失败: {str(e)[:200]}") from e

    logger.info(
        f"[Eval] 评测任务已提交: report={report_id}, testset={body.testset}, "
        f"metrics={metrics}, limit={body.sample_limit}"
    )
    return {
        "success": True,
        "message": "评测任务已提交，请轮询进度",
        "data": {
            "report_id": report_id,
            "testset": body.testset,
            "metrics": metrics,
            "sample_limit": body.sample_limit,
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


@router.delete("/reports/{report_id}")
async def remove_report(report_id: int, user: dict = Depends(require_admin)):
    """删除评测报告（管理员）。"""
    deleted = await delete_eval_report(report_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="评测报告不存在")
    return {"success": True, "message": f"评测报告 #{report_id} 已删除"}
