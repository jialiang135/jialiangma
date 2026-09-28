"""
评测执行器 —— 用 RAGAS 对问答质量做量化评估
=============================================

为什么需要这个模块
------------------
README 里写了"四层防幻觉策略"，其中第 4 层是"评测集验证"，但**评测功能此前
被删除了一半**：运行时代码没了，只留下文档、schema 校验和测试里的悬空引用。
这个模块把它补成真的：跑评测集、算指标、落库、出报告。

指标
----
RAGAS 侧（每条问题的回答 + 检索到的上下文 + 期望答案）：

- ``faithfulness``       回答是否忠于检索到的上下文（最主要的防幻觉指标）
- ``answer_relevancy``   回答与问题的相关度
- ``context_precision``  检索到的上下文里有多少是真正有用的
- ``context_recall``     期望答案的信息是否被检索到

自建侧：

- ``honesty_rate``       对**知识库外问题**，回答是否如实说"没有相关信息"
                         （这是"防幻觉"最直接的行为检验：不知道就说不知道）

关于 RAGAS 的兼容垫片
---------------------
``ragas`` 最新版（0.4.3）在 ``llms/base.py`` 顶层
``from langchain_community.chat_models.vertexai import ChatVertexAI``，
而该模块在 langchain-community 0.4.x（1.x 世代）里已被移除 ——
即 ragas 目前仍绑定在 langchain-core 0.3.x 世代。

我们不为此降级依赖（那会连带破坏推理模型的流式思考输出，见 core/llm.py 的
说明），而是在导入前注入一个**占位模块**。这个 ChatVertexAI 只在 ragas 内部
用于类型标注，实际走 DeepSeek 路径时不会被实例化。
上游修好后删掉 ``_install_ragas_compat_shim()`` 即可。
"""
from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path
from typing import Any

from loguru import logger

from config.settings import settings
from core.database import (
    get_eval_report,
    run_async_from_thread,
    update_eval_report,
)

# 判定"如实回答不知道"的关键词。知识库检索为空时，系统提示词要求回答
# "我的知识库中没有这方面的信息"，这里按同一口径检验。
_REFUSAL_MARKERS = (
    "知识库中没有", "知识库中未找到", "没有相关信息", "未找到相关",
    "没有这方面的信息", "暂无相关", "没有记录", "无法回答",
)

# ── 默认参数（API 路由与评测 Agent 共用同一份，避免两处漂移）──
DEFAULT_TESTSET = "auto_eval"
DEFAULT_METRICS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
# 单次评测默认只跑前 N 条：全量评测很慢（每条要跑一次完整问答 + LLM 判定）
DEFAULT_SAMPLE_LIMIT = 5


def _install_ragas_compat_shim() -> None:
    """注入 ragas 缺失的 ChatVertexAI 占位模块（见模块 docstring）。"""
    module_name = "langchain_community.chat_models.vertexai"
    if module_name in sys.modules:
        return
    try:
        __import__(module_name)
        return
    except ModuleNotFoundError:
        pass

    from langchain_core.language_models.chat_models import BaseChatModel

    shim = types.ModuleType(module_name)
    shim.ChatVertexAI = BaseChatModel  # type: ignore[attr-defined]
    sys.modules[module_name] = shim
    logger.debug("已注入 ragas 兼容垫片: {}", module_name)


def _load_ragas():
    """延迟导入 ragas（它依赖较重，且需要先装垫片）。"""
    _install_ragas_compat_shim()
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (
        answer_relevancy,
        context_precision,
        context_recall,
        faithfulness,
    )

    return {
        "EvaluationDataset": EvaluationDataset,
        "evaluate": evaluate,
        "LangchainLLMWrapper": LangchainLLMWrapper,
        "LangchainEmbeddingsWrapper": LangchainEmbeddingsWrapper,
        "metrics": {
            "faithfulness": faithfulness,
            "answer_relevancy": answer_relevancy,
            "context_precision": context_precision,
            "context_recall": context_recall,
        },
    }


# ------------------------------------------------------------
# 评测集
# ------------------------------------------------------------

def list_testsets() -> list[dict]:
    """列出 assets/test_data 下可用的评测集。"""
    test_dir = settings.resolve_path(settings.test_data_dir)
    out = []
    if not test_dir.exists():
        return out
    for path in sorted(test_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            questions = data.get("questions") or []
            out.append({
                "name": path.stem,
                "title": data.get("name", path.stem),
                "count": len(questions),
            })
        except Exception as e:
            logger.warning("评测集读取失败: {} - {}", path.name, e)
    return out


def _resolve_testset_path(name: str) -> Path:
    """
    解析评测集文件名。

    同时接受 ``auto_eval`` 与 ``auto_eval_testset`` 两种写法 ——
    实际文件名带 ``_testset`` 后缀，但接口参数里带上它很啰嗦，
    两种都支持可以少一类"名字对不上"的报错。
    """
    test_dir = settings.resolve_path(settings.test_data_dir)
    # 只取 basename，防止路径穿越
    safe = Path(name).name
    candidates = [safe]
    if not safe.endswith("_testset"):
        candidates.append(f"{safe}_testset")
    for candidate in candidates:
        path = test_dir / f"{candidate}.json"
        if path.exists():
            return path
    raise FileNotFoundError(f"评测集不存在: {name}")


def load_testset(name: str) -> list[dict]:
    """按名称加载评测集（返回问题列表）。"""
    data = json.loads(_resolve_testset_path(name).read_text(encoding="utf-8"))
    return data.get("questions") or []


# ------------------------------------------------------------
# 逐题生成回答 + 检索上下文
# ------------------------------------------------------------

def _answer_one_sync(question: str, owner_id: int) -> tuple[str, list[str], list[str]]:
    """
    对单条问题跑一次问答图，取回：回答、检索到的上下文、推理步骤。

    **同步函数**，在线程池里执行（内部要跑整张图，包含同步的检索链路）。
    """
    import asyncio

    from agent.graph_workflow import get_agent_graph
    from api.sse_stream import _initial_state

    graph = get_agent_graph()
    state = _initial_state(question, owner_id, "evaluator", "chat")
    final_state = asyncio.run(graph.ainvoke(state))

    answer = final_state.get("final_answer", "") or ""
    contexts = [
        d.get("content", "")
        for d in (final_state.get("retrieved_docs") or [])
        if d.get("content")
    ]
    steps = [s for s in (final_state.get("reasoning_log") or []) if isinstance(s, str)]
    return answer, contexts, steps


def _looks_like_refusal(answer: str) -> bool:
    """回答是否属于"如实说不知道"。"""
    return any(m in answer for m in _REFUSAL_MARKERS)


# ------------------------------------------------------------
# 主流程（后台任务，跑在线程池里）
# ------------------------------------------------------------

def _select_samples(questions: list[dict], limit: int | None) -> list[dict]:
    """
    按分类轮询取样，保证每类都被覆盖。

    直接取前 N 条会有个坑：评测集里"幻觉检测"类（知识库里本就没有答案的问题）
    往往排在最后，小样本评测就永远测不到**诚实度**——而那恰恰是防幻觉最直接的
    指标。所以这里按分类轮流取，让 5 条样本也能覆盖到各类问题。
    """
    if not limit or limit >= len(questions):
        return questions

    buckets: dict[str, list[dict]] = {}
    for q in questions:
        buckets.setdefault(q.get("category", "其他"), []).append(q)

    selected: list[dict] = []
    while len(selected) < limit:
        progressed = False
        for items in buckets.values():
            if not items:
                continue
            selected.append(items.pop(0))
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


def run_eval_task(report_id: int, owner_id: int, testset_name: str,
                  metrics: list[str], limit: int | None = None) -> None:
    """
    执行一次评测并落库。**同步函数**，由线程池调度。

    评测很慢（每条问题要跑一次完整问答图，RAGAS 判定又要额外调多次 LLM），
    因此必须作为后台任务，且默认只跑前 N 条。
    """
    started = time.time()
    try:
        run_async_from_thread(update_eval_report(report_id, status="running", progress=5))

        questions = load_testset(testset_name)
        questions = _select_samples(questions, limit)
        if not questions:
            run_async_from_thread(update_eval_report(
                report_id, status="failed", error="评测集为空", progress=100,
            ))
            return

        run_async_from_thread(update_eval_report(
            report_id, total_questions=len(questions), progress=10,
        ))

        # ── 1. 逐题生成回答 ──
        samples: list[dict] = []
        per_question: list[dict] = []
        for i, item in enumerate(questions, start=1):
            question = item.get("question", "")
            expected = item.get("expected_answer", "") or ""
            category = item.get("category", "")
            try:
                answer, contexts, _steps = _answer_one_sync(question, owner_id)
            except Exception as e:
                logger.error("[Eval] 生成回答失败: {} - {}", question[:40], e)
                answer, contexts = "", []

            refused = _looks_like_refusal(answer)
            per_question.append({
                "id": item.get("id"),
                "category": category,
                "question": question,
                "expected_answer": expected,
                "answer": answer,
                "contexts_count": len(contexts),
                "refused": refused,
            })
            samples.append({
                "user_input": question,
                "response": answer or "（无回答）",
                "retrieved_contexts": contexts or ["（未检索到任何上下文）"],
                "reference": expected or "（无期望答案）",
            })

            progress = 10 + int(45 * i / len(questions))
            run_async_from_thread(update_eval_report(report_id, progress=progress))
            logger.info("[Eval] 已生成 {}/{} : {}", i, len(questions), question[:40])

        # ── 2. RAGAS 指标 ──
        metric_scores: dict[str, Any] = {}
        try:
            rag = _load_ragas()
            from config.settings import get_dashscope_embeddings, get_deepseek_llm

            judge_llm = rag["LangchainLLMWrapper"](
                get_deepseek_llm(temperature=0.0, streaming=False)
            )
            judge_emb = rag["LangchainEmbeddingsWrapper"](get_dashscope_embeddings())

            selected = [rag["metrics"][m] for m in metrics if m in rag["metrics"]]
            if selected:
                dataset = rag["EvaluationDataset"].from_list(samples)
                result = rag["evaluate"](
                    dataset=dataset, metrics=selected, llm=judge_llm, embeddings=judge_emb,
                )
                # ragas 返回的结果对象可转 dict；只保留数值型指标
                raw = result.to_pandas().to_dict(orient="list") \
                    if hasattr(result, "to_pandas") else dict(result)
                for key, value in raw.items():
                    if key in ("user_input", "response", "retrieved_contexts", "reference"):
                        continue
                    try:
                        vals = [v for v in value if isinstance(v, (int, float))]
                        if vals:
                            metric_scores[key] = round(sum(vals) / len(vals), 4)
                    except TypeError:
                        continue
        except Exception as e:
            logger.error("[Eval] RAGAS 指标计算失败: {}", e)
            metric_scores["_error"] = str(e)[:300]

        run_async_from_thread(update_eval_report(report_id, progress=85))

        # ── 3. 自建指标：诚实度 ──
        # "幻觉检测" 类问题 = 知识库中本就没有答案，正确行为是如实说不知道
        hallucination_items = [q for q in per_question
                               if "幻觉" in (q["category"] or "")]
        if hallucination_items:
            honest = sum(1 for q in hallucination_items if q["refused"])
            honesty_rate = round(honest / len(hallucination_items), 4)
            hallucination_count = len(hallucination_items) - honest
        else:
            honesty_rate = None
            hallucination_count = 0

        answered = [q for q in per_question if q["answer"]]
        avg_ctx = round(
            sum(q["contexts_count"] for q in per_question) / len(per_question), 2
        )

        # ── 4. 诊断建议 ──
        recommendations: list[str] = []
        if metric_scores.get("faithfulness") is not None and metric_scores["faithfulness"] < 0.8:
            recommendations.append(
                "faithfulness 偏低：回答与检索内容的吻合度不足，"
                "检查系统提示词的防幻觉约束或降低 temperature"
            )
        if metric_scores.get("context_recall") is not None and metric_scores["context_recall"] < 0.7:
            recommendations.append(
                "context_recall 偏低：期望答案的信息没被检索到，"
                "考虑调大 top_k、开启混合检索或改进分块策略"
            )
        if metric_scores.get("context_precision") is not None and metric_scores["context_precision"] < 0.7:
            recommendations.append(
                "context_precision 偏低：检索结果里无关内容较多，考虑启用 rerank 或调整 top_k"
            )
        if honesty_rate is not None and honesty_rate < 1.0:
            recommendations.append(
                f"诚实度 {honesty_rate:.0%}：有 {hallucination_count} 条知识库外问题"
                "没有如实说明「没有相关信息」，需要加强防幻觉约束"
            )
        if avg_ctx < 1:
            recommendations.append("平均检索到的上下文不足 1 条，知识库可能为空或检索链路异常")

        duration = round(time.time() - started, 1)
        run_async_from_thread(update_eval_report(
            report_id,
            status="done",
            progress=100,
            completed=len(per_question),
            results_json=json.dumps(per_question, ensure_ascii=False),
            recommendations_json=json.dumps(recommendations, ensure_ascii=False),
            metrics_json=json.dumps(metric_scores, ensure_ascii=False),
            honesty_rate=honesty_rate,
            hallucination_count=hallucination_count,
            avg_match_score=metric_scores.get("faithfulness"),
            poor_retrieval_count=sum(1 for q in per_question if q["contexts_count"] == 0),
            answered_count=len(answered),
            duration_seconds=duration,
        ))
        logger.info(
            "[Eval] 评测完成 report={}: {} 题, 指标={}, 耗时 {}s",
            report_id, len(per_question), metric_scores, duration,
        )

    except Exception as e:
        logger.error("[Eval] 评测任务失败: {}", e)
        try:
            run_async_from_thread(update_eval_report(
                report_id, status="failed", progress=100, error=str(e)[:500],
            ))
        except Exception as inner:
            logger.error("[Eval] 标记评测失败也失败了: {}", inner)
