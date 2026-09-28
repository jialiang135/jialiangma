"""
评测 Agent —— 知识库问答质量评测
=================================

注意：真正的评测**不在这里同步执行**。一次评测要对每条问题跑一遍完整问答
图，再由 LLM 逐条判定，可能耗时几分钟到几十分钟 —— 放进图节点里会把整个
请求拖死。因此这个节点只做两件事：

1. 用户明确要求"跑评测"时 → 创建报告记录 + 提交后台任务，立刻返回
2. 否则 → 汇报最近一次评测的结果与可用评测集
"""
from __future__ import annotations

import json

from langchain_core.messages import AIMessage
from loguru import logger

from agent.prompts import EVAL_AGENT_SYSTEM_PROMPT  # noqa: F401  (供上层/文档引用)
from agent.state import AgentState
from core.database import (
    create_eval_report,
    list_eval_reports,
    update_eval_report,
)
from core.eval_runner import DEFAULT_METRICS, DEFAULT_SAMPLE_LIMIT, DEFAULT_TESTSET

# 触发"运行评测"意图的关键词
_RUN_KEYWORDS = ("运行评测", "跑评测", "执行评测", "开始评测", "重新评测", "评测一下")


def _wants_run(query: str) -> bool:
    return any(k in query for k in _RUN_KEYWORDS)


def _format_report(report: dict) -> str:
    """把报告渲染成 Markdown。"""
    metrics = {}
    if report.get("metrics_json"):
        try:
            metrics = json.loads(report["metrics_json"])
        except json.JSONDecodeError:
            pass

    lines = [
        f"## 评测报告 #{report['id']}",
        f"- 评测集: `{report['testset_name']}`",
        f"- 状态: {report['status']}（进度 {report['progress']}%）",
        f"- 题目: {report['completed']}/{report['total_questions']} 已完成",
    ]
    if report.get("duration_seconds"):
        lines.append(f"- 耗时: {report['duration_seconds']}s")

    if metrics:
        lines += ["", "### RAGAS 指标"]
        name_map = {
            "faithfulness": "忠实度（回答是否忠于检索内容，防幻觉核心指标）",
            "answer_relevancy": "相关度（回答是否答到点上）",
            "context_precision": "上下文精确率（检索内容里有用的比例）",
            "context_recall": "上下文召回率（期望答案是否被检索到）",
        }
        for key, value in metrics.items():
            if key.startswith("_"):
                lines.append(f"- ⚠️ {value}")
                continue
            label = name_map.get(key, key)
            lines.append(f"- **{label}**: {value}")

    if report.get("honesty_rate") is not None:
        lines.append(
            f"- **诚实度**（知识库外问题如实说不知道的比例）: {report['honesty_rate']:.0%}"
        )

    if report.get("recommendations_json"):
        try:
            recs = json.loads(report["recommendations_json"])
            if recs:
                lines += ["", "### 改进建议"]
                lines += [f"{i}. {r}" for i, r in enumerate(recs, start=1)]
        except json.JSONDecodeError:
            pass

    if report.get("error"):
        lines += ["", f"❌ 错误: {report['error']}"]

    return "\n".join(lines)


async def eval_agent_node(state: AgentState) -> dict:
    """
    评测 Agent 节点。

    - 用户要求跑评测 → 提交后台任务并立刻返回
    - 否则 → 汇报最近一次结果
    """
    owner_id = state.get("owner_id", 1)
    query = state.get("user_query", "") or ""
    logger.info("[EvalAgent] owner_id={}, query='{}'", owner_id, query[:60])

    reasoning: list[str] = []

    try:
        if _wants_run(query):
            # 提交后台评测任务
            from core.async_queue import async_queue
            from core.eval_runner import run_eval_task

            testset = DEFAULT_TESTSET
            for candidate in ("auto_eval", "ai_interview", "sample_eval"):
                if candidate in query:
                    testset = candidate
                    break

            report_id = await create_eval_report(owner_id, testset, 0)
            try:
                async_queue.enqueue(
                    run_eval_task, report_id, owner_id, testset,
                    DEFAULT_METRICS, DEFAULT_SAMPLE_LIMIT,
                )
            except Exception as e:
                await update_eval_report(
                    report_id, status="failed", error=f"任务提交失败: {str(e)[:200]}"
                )
                raise

            reasoning.append(f"📊 评测任务已提交（报告 #{report_id}，评测集 {testset}）")
            message = (
                f"## 评测任务已提交\n\n"
                f"- 报告编号: **#{report_id}**\n"
                f"- 评测集: `{testset}`\n"
                f"- 指标: {', '.join(DEFAULT_METRICS)}\n"
                f"- 样本数: 前 {DEFAULT_SAMPLE_LIMIT} 题\n\n"
                f"评测需要对每题跑一次完整问答并让 LLM 逐条判定，比较慢。\n"
                f"可在「知识库评测」页面查看进度与结果，或稍后再问我一次。"
            )
            return {
                "final_answer": message,
                "operation_result": message,
                "reasoning_log": reasoning,
                "messages": [AIMessage(content=message)],
            }

        # 汇报最近一次结果
        reports = await list_eval_reports(owner_id, limit=1)
        if not reports:
            reasoning.append("📭 暂无评测记录")
            message = (
                "## 还没有评测记录\n\n"
                "我可以对知识库做量化评测，指标包括：\n"
                "- **忠实度 / 相关度 / 上下文精确率 / 上下文召回率**（RAGAS）\n"
                "- **诚实度**：对知识库外的问题，是否如实回答「没有相关信息」\n\n"
                "说一句「运行评测」我就开始跑。"
            )
        else:
            latest = reports[0]
            reasoning.append(f"📊 最近一次评测: #{latest['id']} ({latest['status']})")
            message = _format_report(latest)
            if latest["status"] in ("pending", "running"):
                message += (
                    f"\n\n⏳ 该报告仍在进行中（进度 {latest['progress']}%），"
                    f"稍后再来查看。"
                )

        return {
            "final_answer": message,
            "operation_result": message,
            "reasoning_log": reasoning,
            "messages": [AIMessage(content=message)],
        }

    except Exception as e:
        logger.error("[EvalAgent] 失败: {}", e)
        message = f"❌ 评测操作失败: {str(e)[:200]}"
        return {
            "final_answer": message,
            "operation_result": message,
            "reasoning_log": reasoning + [f"❌ 错误: {str(e)[:200]}"],
            "messages": [AIMessage(content=message)],
        }
