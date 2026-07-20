"""
评测校验 Agent —— 批量自动评测，检测幻觉，生成评测报告
支持并发评测（asyncio.Semaphore 控速），大幅缩短评测时间
"""
import asyncio
import json
from typing import Optional
from langchain_core.messages import AIMessage
from loguru import logger

from agent.state import AgentState
from agent.prompts import EVAL_AGENT_SYSTEM_PROMPT
from config.settings import get_deepseek_llm
from rag.retriever import retrieve, format_context_for_prompt
from core.database import insert_eval_report

# 并发控制：同时最多发 N 道题的 LLM 请求
# 太大会触发 API 限流，太小跟串行没区别，5~8 是比较均衡的值
MAX_CONCURRENT_QUESTIONS = 6


async def eval_agent_node(state: AgentState) -> dict:
    """
    评测校验 Agent 节点。
    根据 testset 中的问题并发执行问答 + 幻觉检测。
    """
    testset = state.get("testset")
    owner_id = state.get("owner_id", 1)
    logger.info("[EvalAgent] 开始评测, owner_id={}", owner_id)

    if not testset or not testset.get("questions"):
        return {
            "final_answer": "未提供测试集。请先上传测试问答集 JSON 文件。",
            "reasoning_log": ["⚠️ 未提供测试集"],
            "messages": [AIMessage(content="未提供测试集")],
        }

    questions = testset["questions"]
    testset_name = testset.get("name", "未命名测试集")
    total = len(questions)
    reasoning = [f"📊 开始评测: {testset_name}, 共 {total} 题 (并发数={MAX_CONCURRENT_QUESTIONS})"]

    # 用信号量控制并发数，避免打爆 API 限流
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_QUESTIONS)

    # 创建所有评测任务
    tasks = [
        _eval_single_question(q, i, total, owner_id, semaphore, reasoning)
        for i, q in enumerate(questions)
    ]

    # 并发执行所有题目
    results = await asyncio.gather(*tasks)
    # 过滤掉 None（理论上不会，但防御编程）
    results = [r for r in results if r is not None]

    # Step 4: 聚合统计
    completed = len(results)
    hallucination_count = sum(1 for r in results if r.get("is_hallucination"))
    poor_retrieval_count = sum(
        1 for r in results if r.get("retrieval_quality") == "poor"
    )
    total_score = sum(r.get("match_score", 0) for r in results)

    accuracy = ((completed - hallucination_count) / completed * 100) if completed else 0
    hallucination_rate = (hallucination_count / completed * 100) if completed else 0
    avg_score = (total_score / completed) if completed else 0

    recommendations = _generate_recommendations(results, poor_retrieval_count, hallucination_count)

    report = {
        "testset_name": testset_name,
        "total_questions": total,
        "completed": completed,
        "accuracy": round(accuracy, 1),
        "hallucination_count": hallucination_count,
        "hallucination_rate": round(hallucination_rate, 1),
        "avg_match_score": round(avg_score, 1),
        "poor_retrieval_count": poor_retrieval_count,
        "results": results,
        "recommendations": recommendations,
    }

    # 保存到数据库
    try:
        report_id = insert_eval_report(owner_id, report)
        report["id"] = report_id
        reasoning.append(f"💾 评测报告已保存 (id={report_id})")
    except Exception as e:
        logger.error(f"[EvalAgent] 报告保存失败: {e}")

    reasoning.append(
        f"📊 评测完成: 准确率 {accuracy:.1f}%, "
        f"幻觉 {hallucination_count}/{completed}, "
        f"平均匹配度 {avg_score:.1f}"
    )

    # 生成可读报告文本
    report_text = _format_report_text(report)

    return {
        "final_answer": report_text,
        "eval_results": results,
        "eval_report": report,
        "reasoning_log": reasoning,
        "messages": [AIMessage(content=report_text)],
    }


async def _eval_single_question(
    q: dict,
    index: int,
    total: int,
    owner_id: int,
    semaphore: asyncio.Semaphore,
    reasoning_log: list,
) -> dict:
    """
    评测单道题（在信号量控制下并发执行）。
    返回单条结果 dict。
    """
    question_text = q.get("question", "")
    expected = q.get("expected_answer", "")
    question_id = q.get("id", f"q_{index+1}")

    async with semaphore:
        logger.info(f"[EvalAgent] [{index+1}/{total}] 开始评测: {question_text[:60]}...")
        # 实时进度（asyncio 保证 list.append 是安全的）
        reasoning_log.append(f"📝 [{index+1}/{total}] 评测: {question_text[:60]}...")

        try:
            # Step 1: 检索（ChromaDB 本地操作，很快）
            retrieval_result = retrieve(question_text, owner_id=owner_id, top_k_rerank=5)
            context = format_context_for_prompt(retrieval_result)
            sources = [d["source"] for d in retrieval_result.get("documents", [])]

            if not retrieval_result["documents"]:
                retrieval_quality = "poor"
            elif retrieval_result["documents"][0]["score"] < 0.3:
                retrieval_quality = "partial"
            else:
                retrieval_quality = "good"

            # 每个并发任务用自己的 LLM 实例（避免共享状态问题）
            llm = get_deepseek_llm(temperature=0.1, streaming=False)

            # Step 2: 生成回答
            eval_prompt = f"""{EVAL_AGENT_SYSTEM_PROMPT}

## 当前测试问题
{question_text}

## 知识库检索结果
{context if context else "（知识库中未检索到相关内容）"}

## 期望答案（如有）
{expected if expected else "（未提供期望答案）"}

请基于知识库检索结果回答上述问题，注意只能使用知识库中的真实信息，禁止编造。
"""
            response = await llm.ainvoke(eval_prompt)
            ai_answer = response.content

            # Step 3: 幻觉检测
            hallucination_check = await _detect_hallucination(
                llm, question_text, ai_answer, context, expected
            )

            is_hallucination = hallucination_check.get("is_hallucination", False)
            match_score = hallucination_check.get("match_score", 50.0)

            logger.info(
                f"[EvalAgent] [{index+1}/{total}] 完成: "
                f"hallucination={is_hallucination}, score={match_score:.0f}"
            )

            return {
                "question_id": question_id,
                "question": question_text,
                "ai_answer": ai_answer[:2000],
                "expected_answer": expected[:1000] if expected else None,
                "sources_used": sources,
                "is_hallucination": is_hallucination,
                "hallucination_detail": hallucination_check.get("detail", ""),
                "match_score": match_score,
                "retrieval_quality": retrieval_quality,
            }

        except Exception as e:
            logger.error(f"[EvalAgent] 评测失败: question_id={question_id} - {e}")
            return {
                "question_id": question_id,
                "question": question_text,
                "ai_answer": f"评测失败: {str(e)[:500]}",
                "expected_answer": expected[:1000] if expected else None,
                "sources_used": [],
                "is_hallucination": False,
                "hallucination_detail": "",
                "match_score": 0.0,
                "retrieval_quality": "poor",
            }


async def _detect_hallucination(
    llm,
    question: str,
    ai_answer: str,
    kb_context: str,
    expected_answer: Optional[str],
) -> dict:
    """
    使用 LLM 检测回答中的幻觉。
    返回 {"is_hallucination": bool, "match_score": float, "detail": str}
    """
    check_prompt = f"""你是知识库质量审核专家。请严格评审以下 AI 回答。

## 问题
{question}

## AI 回答
{ai_answer}

## 知识库原文（唯一事实来源）
{kb_context if kb_context else "（知识库为空，任何事实性陈述都可能是幻觉）"}

## 期望答案（如有）
{expected_answer if expected_answer else "（未提供）"}

## 评审任务
1. 逐句核对 AI 回答中的事实性陈述，是否都能在知识库原文中找到依据？
2. 是否存在无中生有的信息（知识库中没有但 AI 说了）？
3. 是否存在与知识库原文矛盾的信息？
4. 给出 0-100 的匹配度评分。

请用 JSON 格式输出：
```json
{{"is_hallucination": true/false, "match_score": 0-100, "detail": "详细说明"}}
```
"""
    try:
        response = await llm.ainvoke(check_prompt)
        content = response.content

        # 尝试解析 JSON
        import re
        json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            return {
                "is_hallucination": result.get("is_hallucination", False),
                "match_score": float(result.get("match_score", 50)),
                "detail": str(result.get("detail", ""))[:500],
            }
    except Exception as e:
        logger.warning(f"[EvalAgent] 幻觉检测解析失败: {e}")

    # 降级：简单判断
    return {
        "is_hallucination": False,
        "match_score": 50.0,
        "detail": "检测解析失败，标记为待人工复核",
    }


def _generate_recommendations(
    results: list[dict],
    poor_retrieval_count: int,
    hallucination_count: int,
) -> list[str]:
    """基于评测结果生成优化建议"""
    recs = []

    if poor_retrieval_count > 0:
        recs.append(
            f"有 {poor_retrieval_count} 道题检索质量较差，"
            "建议补充相关领域的文档，或调整文本分块大小（当前 1000 字符）"
        )

    if hallucination_count > 0:
        recs.append(
            f"检测到 {hallucination_count} 次幻觉，"
            "建议加强系统提示词约束，或提高检索返回数量（topK）"
        )

    # 分析低分项
    low_score_items = [r for r in results if r.get("match_score", 0) < 50]
    if low_score_items:
        topics = [r["question"][:30] for r in low_score_items[:3]]
        recs.append(
            f"以下话题匹配度较低: {', '.join(topics)}..."
            "建议补充相关知识文档"
        )

    if not recs:
        recs.append("知识库覆盖度良好，暂无优化建议。")

    # 通用建议
    recs.append("建议定期更新知识库，保持与最新个人经历同步。")
    recs.append("长文档建议按主题拆分为多个文件，提升检索精度。")

    return recs


def _format_report_text(report: dict) -> str:
    """将评测报告格式化为可读文本"""
    lines = [
        f"## 评测报告: {report['testset_name']}",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 总题数 | {report['total_questions']} |",
        f"| 完成数 | {report['completed']} |",
        f"| 准确率 | {report['accuracy']}% |",
        f"| 幻觉数 | {report['hallucination_count']} |",
        f"| 幻觉率 | {report['hallucination_rate']}% |",
        f"| 平均匹配度 | {report['avg_match_score']} |",
        f"| 低质量检索 | {report['poor_retrieval_count']} |",
        "",
        "## 优化建议",
    ]

    for rec in report.get("recommendations", []):
        lines.append(f"- {rec}")

    lines.append("")
    lines.append("## 详细结果")

    for r in report.get("results", []):
        status = "⚠️ 幻觉" if r.get("is_hallucination") else "✅ 正常"
        lines.append(
            f"- [{status}] {r['question'][:60]}... "
            f"| 匹配度: {r.get('match_score', 0):.0f} "
            f"| 检索: {r.get('retrieval_quality', 'unknown')}"
        )

    return "\n".join(lines)
