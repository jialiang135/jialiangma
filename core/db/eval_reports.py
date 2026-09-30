"""
core/db/eval_reports.py —— 评测报告（RAGAS 指标、诚实度、诊断建议）。
================================================

由 core/database.py 拆分而来（2026-09；拆分前是单文件 1366 行 / 57 个函数）。
数据库层只有 `core.db` 这一个入口，没有兼容门面 —— 直接从这里导入。
"""

from __future__ import annotations

from sqlalchemy import (
    delete,
    select,
    update,
)

from core.db.engine import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    session_scope,
)
from core.db.models import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    EvalReport,
)


def _eval_to_dict(report: EvalReport) -> dict:
    return {
        "id": report.id,
        "owner_id": report.owner_id,
        "testset_name": report.testset_name,
        "status": report.status,
        "progress": report.progress,
        "total_questions": report.total_questions,
        "completed": report.completed,
        "answered_count": report.answered_count,
        "hallucination_count": report.hallucination_count,
        "hallucination_rate": report.hallucination_rate,
        "honesty_rate": report.honesty_rate,
        "avg_match_score": report.avg_match_score,
        "poor_retrieval_count": report.poor_retrieval_count,
        "duration_seconds": report.duration_seconds,
        "metrics_json": report.metrics_json,
        "results_json": report.results_json,
        "recommendations_json": report.recommendations_json,
        "config_json": report.config_json,
        "error": report.error,
        "created_at": report.created_at,
    }


# 允许通过 update_eval_report 更新的字段（白名单，防止拼错字段名却不报错）

_EVAL_UPDATABLE = {
    "status",
    "progress",
    "total_questions",
    "completed",
    "answered_count",
    "hallucination_count",
    "hallucination_rate",
    "honesty_rate",
    "avg_match_score",
    "poor_retrieval_count",
    "duration_seconds",
    "metrics_json",
    "results_json",
    "recommendations_json",
    "config_json",
    "error",
}


async def create_eval_report(owner_id: int, testset_name: str, total_questions: int = 0) -> int:
    async with session_scope() as session:
        report = EvalReport(
            owner_id=owner_id,
            testset_name=testset_name,
            total_questions=total_questions,
            status="pending",
        )
        session.add(report)
        await session.flush()
        return report.id


async def update_eval_report(report_id: int, **fields) -> None:
    values = {k: v for k, v in fields.items() if k in _EVAL_UPDATABLE}
    if not values:
        return
    async with session_scope() as session:
        await session.execute(update(EvalReport).where(EvalReport.id == report_id).values(**values))


async def get_eval_report(report_id: int, owner_id: int | None = None) -> dict | None:
    async with session_scope() as session:
        stmt = select(EvalReport).where(EvalReport.id == report_id)
        if owner_id is not None:
            stmt = stmt.where(EvalReport.owner_id == owner_id)
        report = (await session.execute(stmt)).scalar_one_or_none()
        return _eval_to_dict(report) if report else None


async def list_eval_reports(owner_id: int | None = None, limit: int = 20) -> list[dict]:
    """列出评测报告（不含体积大的 results_json）。"""
    async with session_scope() as session:
        stmt = select(EvalReport)
        if owner_id is not None:
            stmt = stmt.where(EvalReport.owner_id == owner_id)
        rows = (
            (await session.execute(stmt.order_by(EvalReport.created_at.desc()).limit(limit)))
            .scalars()
            .all()
        )
        out = []
        for r in rows:
            item = _eval_to_dict(r)
            item.pop("results_json", None)  # 逐题明细只在详情接口返回
            out.append(item)
        return out


async def delete_eval_report(report_id: int) -> bool:
    """删除评测报告，返回是否删到了行。"""
    async with session_scope() as session:
        result = await session.execute(delete(EvalReport).where(EvalReport.id == report_id))
        return (result.rowcount or 0) > 0


# ============================================================
# 备份
# ============================================================
