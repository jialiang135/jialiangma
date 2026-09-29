#!/usr/bin/env python
"""
检索质量离线评测（hit@k / MRR，支持 A/B 对比）
================================================

**这个脚本要回答的问题**：知识库检索到底准不准？开/关混合检索、开/关重排，
指标差多少？——把"检索效果"从一句主观感觉变成可以贴到简历/答辩上的数字。

只评**检索层**，不调 LLM：对每条问题跑一次 ``rag.retriever.retrieve()``，
看标注的期望文档有没有出现在 top-k 里。因此快、免费（只花 embedding + rerank
的钱），与 RAGAS 那套端到端评测互补。

指标定义
--------
- ``hit@k``：期望文档中**任意一个**出现在前 k 条结果里的题数占比。
- ``MRR``：Mean Reciprocal Rank —— 第一条命中结果排名的倒数（第 1 名记 1.0、
  第 2 名记 0.5……），未命中记 0；对全部**可答题**取平均。
- 无答案题（``expected_sources`` 为空）：不参与 hit/MRR，单独统计
  ``empty_rate``（检索返回 0 条的比例）与 ``mean_top_score``（top-1 分数），
  用来观察"知识库里没有的问题，系统会不会硬塞不相关内容"。

检索参数怎么注入（关键）
------------------------
``retrieve()`` 的默认值是在函数内部读全局 ``settings`` 的，评测时**不能**直接改
``settings``（同进程还在跑真实用户请求，改了会串味）。所以这里**复用**
``core/eval_runner.py`` 已经建好的 contextvar 机制 ``applied_retrieve_overrides``：
它把覆盖参数按线程/上下文隔离地注入 ``retrieve()``，退出即还原，全局 settings
一字未动。A/B 就是给两组覆盖参数各跑一遍。

用法
----
    # 冒烟：先跑 10 条确认链路通
    python scripts/eval_retrieval.py --limit 10

    # A/B：混合检索 开 vs 关
    python scripts/eval_retrieval.py \
        --a use_hybrid_search=true --b use_hybrid_search=false

    # 更多变量：关重排、调候选数
    python scripts/eval_retrieval.py \
        --a "use_hybrid_search=true,top_k_search=20" \
        --b "use_hybrid_search=true,top_k_search=20,top_k_rerank=5"

    # 让 A/B 命中同一缓存口径（默认评测时**关闭**检索缓存，测真实耗时）
    python scripts/eval_retrieval.py --cache --a use_hybrid_search=true --b use_hybrid_search=false

覆盖键取值：``use_hybrid_search`` / ``use_search_cache``（true|false）、
``top_k_search`` / ``top_k_rerank``（整数）、``bm25_weight``（小数）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

# 让脚本无论从哪里被调用（`python scripts/eval_retrieval.py` 时 sys.path[0]
# 是 scripts/，找不到 config/rag/core）都能 import 到项目包。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ============================================================
# 纯逻辑（无副作用、不联网、不读配置 —— 供离线单测）
# ============================================================


def first_hit_rank(sources: list[str], expected: list[str]) -> int | None:
    """
    返回第一个命中的期望文档的**排名**（1-based）；未命中或没有期望时返回 ``None``。

    边界：
    - ``expected`` 为空 → 这是"无答案题"，没有可命中目标，返回 ``None``。
    - ``sources`` 为空 → 没检索到任何东西，返回 ``None``。
    """
    if not expected:
        return None
    expected_set = set(expected)
    for rank, source in enumerate(sources, start=1):
        if source in expected_set:
            return rank
    return None


def hit_at_k(sources: list[str], expected: list[str], k: int) -> bool:
    """期望文档是否出现在前 k 条里（``k`` 为边界时含第 k 名）。"""
    rank = first_hit_rank(sources, expected)
    return rank is not None and rank <= k


def reciprocal_rank(sources: list[str], expected: list[str]) -> float:
    """第一条命中结果排名的倒数；未命中为 0.0。"""
    rank = first_hit_rank(sources, expected)
    return 1.0 / rank if rank else 0.0


def score_question(
    document_sources: list[str],
    expected_sources: list[str],
    ks: tuple[int, ...] = (1, 3, 5),
    top_score: float | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """
    单题打分。返回一个可聚合的 dict：

    - 可答题（``expected_sources`` 非空）：含 ``hit@k`` 各键、``rr``、``rank``。
    - 无答案题（``expected_sources`` 为空）：``is_no_answer=True``，只记
      ``returned``（返回条数）与 ``top_score``。
    - ``error`` 非空：检索链路故障，记 ``is_error=True``（既不算命中也不算
      "确实没有" —— 与 ``rag/retriever.py`` 的契约一致）。
    """
    record: dict[str, Any] = {
        "returned": len(document_sources),
        "top_score": top_score,
        "error": error,
    }
    if error is not None:
        record["is_error"] = True
        record["is_no_answer"] = False
    elif not expected_sources:
        record["is_error"] = False
        record["is_no_answer"] = True
    else:
        record["is_error"] = False
        record["is_no_answer"] = False
        rank = first_hit_rank(document_sources, expected_sources)
        for k in ks:
            record[f"hit@{k}"] = hit_at_k(document_sources, expected_sources, k)
        record["rank"] = rank
        record["rr"] = 1.0 / rank if rank else 0.0
    return record


def aggregate(records: list[dict[str, Any]], ks: tuple[int, ...] = (1, 3, 5)) -> dict[str, Any]:
    """
    聚合单题结果。

    分组：
    - 可答题 → ``hit@k`` 均值 与 ``mrr``（均值）。
    - 无答案题 → ``no_answer_empty_rate``（返回 0 条的比例）、``no_answer_mean_top_score``。
    - 故障题（error）单独计数，不混进上面任何一组。
    """
    answerable = [r for r in records if not r.get("is_no_answer") and not r.get("is_error")]
    no_answer = [r for r in records if r.get("is_no_answer")]
    errors = [r for r in records if r.get("is_error")]

    out: dict[str, Any] = {
        "total": len(records),
        "answerable": len(answerable),
        "no_answer": len(no_answer),
        "errors": len(errors),
    }
    for k in ks:
        key = f"hit@{k}"
        vals = [1.0 if r.get(key) else 0.0 for r in answerable]
        out[key] = round(statistics.fmean(vals), 4) if vals else 0.0
    rrs = [r.get("rr", 0.0) for r in answerable]
    out["mrr"] = round(statistics.fmean(rrs), 4) if rrs else 0.0

    if no_answer:
        empty = sum(1 for r in no_answer if r.get("returned") == 0)
        out["no_answer_empty_rate"] = round(empty / len(no_answer), 4)
        scored = [r["top_score"] for r in no_answer if r.get("top_score") is not None]
        out["no_answer_mean_top_score"] = round(statistics.fmean(scored), 4) if scored else None
    else:
        out["no_answer_empty_rate"] = None
        out["no_answer_mean_top_score"] = None

    if errors:
        out["error_messages"] = sorted({str(r.get("error")) for r in errors})
    return out


_NUMERIC_METRICS = ("hit@1", "hit@3", "hit@5", "mrr")


def metric_delta(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """
    逐指标计算 ``b - a``（只对两边都存在的数值指标求差）。
    用于 A/B 报告里的 "Δ(B-A)" 列。
    """
    delta: dict[str, Any] = {}
    for key in _NUMERIC_METRICS:
        if isinstance(a.get(key), (int, float)) and isinstance(b.get(key), (int, float)):
            delta[key] = round(b[key] - a[key], 4)
    return delta


# ============================================================
# CLI 参数解析辅助（纯逻辑）
# ============================================================


def _coerce(raw: str) -> Any:
    """把命令行字符串转成合适的 Python 类型：bool / int / float / str。"""
    low = raw.strip().lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw.strip()


def parse_overrides(spec: str | None) -> dict[str, Any]:
    """
    把 ``"use_hybrid_search=true,top_k_search=20"`` 解析成
    ``{"use_hybrid_search": True, "top_k_search": 20}``。空串/None → ``{}``。
    """
    if not spec:
        return {}
    out: dict[str, Any] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"覆盖参数格式应为 key=value，收到: {part!r}")
        key, _, value = part.partition("=")
        out[key.strip()] = _coerce(value)
    return out


def parse_ks(spec: str) -> tuple[int, ...]:
    """``"1,3,5"`` → ``(1, 3, 5)``。"""
    ks = tuple(sorted({int(x) for x in spec.split(",") if x.strip()}))
    if not ks or ks[0] < 1:
        raise ValueError(f"非法 --ks: {spec!r}")
    return ks


# ============================================================
# 数据加载
# ============================================================


def load_cases(path: Path, limit: int | None) -> list[dict[str, Any]]:
    """读取评测集 JSON，返回 ``{"questions": [...]}`` 里的问题列表。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data.get("questions") or []
    if limit is not None:
        questions = questions[:limit]
    return questions


def default_testset_path() -> Path:
    """默认评测集：assets/test_data/retrieval_testset.json（相对项目根）。"""
    root = Path(__file__).resolve().parent.parent
    return root / "assets" / "test_data" / "retrieval_testset.json"


# ============================================================
# 执行（有副作用：读配置 / 调 API）
# ============================================================


def _extract(documents: list[dict]) -> tuple[list[str], float | None]:
    """从 retrieve() 的 documents 里取 source 列表与 top-1 分数。"""
    sources = [d.get("source") for d in documents]
    top_score = documents[0].get("score") if documents else None
    return sources, top_score


def run_config(
    questions: list[dict[str, Any]],
    overrides: dict[str, Any],
    owner_id: int,
    ks: tuple[int, ...],
) -> dict[str, Any]:
    """
    用一组检索覆盖参数跑完整评测集，返回 ``{"metrics", "per_question", "latency"}``。

    关键：整段包在 ``applied_retrieve_overrides`` 里 —— contextvar 只在**本线程/
    本上下文**生效，全局 settings 不受影响，也不会泄漏给别的线程。
    """
    # 延迟导入：让本模块可在不联网、不读配置的情况下被单测导入
    from core.eval_runner import applied_retrieve_overrides, validate_retrieve_overrides

    validate_retrieve_overrides(overrides)

    import rag.retriever as retriever_mod

    # 评测时默认关缓存（除非调用方显式要求），保证测的是真实检索耗时，
    # 也避免 A/B 两组之间因缓存命中导致耗时/结果串味。
    effective = {"use_search_cache": False}
    effective.update(overrides)
    validate_retrieve_overrides(effective)

    # 需要看到第 max(ks) 名，才谈得上 hit@max(ks)；若调用方没显式指定
    # top_k_rerank，则把它抬到 max(ks)（覆盖参数优先级更高，仍会生效）。
    default_rerank = max(ks)
    records: list[dict[str, Any]] = []
    latencies: list[float] = []
    per_question: list[dict[str, Any]] = []
    max_k = max(ks)

    with applied_retrieve_overrides(effective):
        # 在 with 内取属性：此时钩子已安装，拿到的是"覆盖感知"包装版
        retrieve = retriever_mod.retrieve
        for i, case in enumerate(questions, start=1):
            question = case["question"]
            expected = case.get("expected_sources") or []
            t0 = time.perf_counter()
            try:
                result = retrieve(
                    question,
                    owner_id,
                    top_k_rerank=default_rerank,
                )
            except Exception as e:  # 单题异常不该中断整轮评测
                result = {"documents": [], "error": f"调用异常: {e}"}
            latencies.append(time.perf_counter() - t0)

            documents = result.get("documents") or []
            sources, top_score = _extract(documents)
            rec = score_question(
                sources,
                expected,
                ks=ks,
                top_score=top_score,
                error=result.get("error"),
            )
            rec["id"] = case.get("id")
            rec["category"] = case.get("category")
            rec["degraded"] = result.get("degraded")
            rec["expected"] = expected
            rec["sources"] = sources
            rec["rank"] = rec.get("rank")
            records.append(rec)
            per_question.append(rec)
            # 单题进度（只在终端给个心跳，别刷屏）
            if len(questions) >= 20 and i % 10 == 0:
                print(f"    ... {i}/{len(questions)}", file=sys.stderr)

    metrics = aggregate(records, ks=ks)
    metrics["mean_latency_s"] = round(statistics.fmean(latencies), 3) if latencies else 0.0
    metrics["max_rerank"] = max_k
    return {"metrics": metrics, "per_question": per_question}


# ============================================================
# 报告渲染
# ============================================================


def _fmt(value: Any, width: int = 10) -> str:
    if value is None:
        return "—".ljust(width)
    if isinstance(value, float):
        return f"{value:.4f}".ljust(width)
    return str(value).ljust(width)


def render_report(title: str, runs: list[tuple[str, dict]], ks: tuple[int, ...]) -> str:
    """
    渲染人类可读报告。``runs`` 是 ``[(label, run_result), ...]``：
    单个 = 单配置报告；两个 = A/B 并排 + Δ(B-A)。
    """
    lines: list[str] = []
    sep = "=" * 72
    lines.append(sep)
    lines.append(title)
    lines.append(sep)

    metric_rows = [*(f"hit@{k}" for k in ks), "mrr"]
    extra_rows = ["no_answer_empty_rate", "no_answer_mean_top_score", "mean_latency_s"]

    if len(runs) == 1:
        label, run = runs[0]
        m = run["metrics"]
        lines.append(f"配置: {label}")
        lines.append(f"可答题 {m['answerable']} / 无答案题 {m['no_answer']} / 故障 {m['errors']}")
        lines.append("-" * 72)
        for key in metric_rows + extra_rows:
            lines.append(f"  {key:<28} {_fmt(m.get(key))}")
    else:
        (label_a, run_a), (label_b, run_b) = runs
        ma, mb = run_a["metrics"], run_b["metrics"]
        delta = metric_delta(ma, mb)

        col = 22
        header = (
            f"{'指标':<24}"
            f"{('A: ' + label_a):<{col}}"
            f"{('B: ' + label_b):<{col}}"
            f"Δ(B-A)"
        )
        lines.append(header)
        lines.append("-" * 72)
        for key in metric_rows + extra_rows:
            lines.append(
                f"{key:<24}"
                f"{_fmt(ma.get(key), col)}"
                f"{_fmt(mb.get(key), col)}"
                f"{_fmt(delta.get(key))}"
            )
        lines.append("-" * 72)
        lines.append(
            f"样本: 可答题 {ma['answerable']} | 无答案题 {ma['no_answer']} | "
            f"A 故障 {ma['errors']} / B 故障 {mb['errors']}"
        )
        if ma.get("error_messages"):
            lines.append(f"A 故障信息: {ma['error_messages']}")
        if mb.get("error_messages"):
            lines.append(f"B 故障信息: {mb['error_messages']}")
        lines.append("")
        lines.append("Δ 说明: 正数=B 优于 A，负数=B 劣于 A（MRR/hit@k 越高越好，latency 越低越好）")

    return "\n".join(lines)


def render_misses(run: dict, ks: tuple[int, ...]) -> str:
    """列出未命中的可答题 + 无答案题里仍返回了文档的题，便于人工诊断。"""
    ks = ks or (1, 3, 5)
    top_k = max(ks)
    lines = ["", f"未命中/异常明细（可答题未进 top-{top_k}；无答案题仍返回文档）:"]
    shown = 0
    for rec in run["per_question"]:
        if rec.get("is_error"):
            lines.append(f"  [故障] {rec['id']}: {rec.get('error')}")
            shown += 1
        elif rec.get("is_no_answer"):
            if rec.get("returned", 0) > 0:
                top = rec.get("top_score")
                lines.append(f"  [无答案却返回{rec['returned']}条, top={top}] {rec['id']}")
                shown += 1
        elif rec.get("rank") is None or rec["rank"] > top_k:
            rank = rec.get("rank")
            lines.append(f"  [未命中 rank={rank}] {rec['id']} 期望={rec['expected']}")
            shown += 1
    if shown == 0:
        lines.append("  （无）")
    return "\n".join(lines)


# ============================================================
# main
# ============================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检索质量评测（hit@k / MRR），支持 A/B 对比",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--testset", default=str(default_testset_path()),
                        help="评测集 JSON 路径（默认 assets/test_data/retrieval_testset.json）")
    parser.add_argument("--owner-id", type=int, default=None,
                        help="知识库所有者 ID（默认取 settings.shared_kb_owner_id）")
    parser.add_argument("--ks", default="1,3,5", help="hit@k 的 k 列表，默认 1,3,5")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟用）")
    parser.add_argument("--a", default=None, help="A 组覆盖参数，如 use_hybrid_search=true")
    parser.add_argument("--b", default=None, help="B 组覆盖参数（给了才做 A/B 对比）")
    parser.add_argument("--label-a", default="A", help="A 组显示名")
    parser.add_argument("--label-b", default="B", help="B 组显示名")
    parser.add_argument("--cache", action="store_true",
                        help="启用检索缓存（默认关闭，测真实耗时）")
    parser.add_argument("--show-misses", action="store_true", help="打印未命中明细")
    parser.add_argument("--json", dest="json_out", default=None, help="把结果另存为 JSON")
    args = parser.parse_args(argv)

    # 中文输出：显式声明 UTF-8，别依赖 Windows 控制台默认 GBK
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    from config.settings import settings

    # 关掉逐条检索日志（loguru 默认会把每条 DEBUG/INFO 打到 stderr，把报告淹没）。
    # 只在本脚本进程内生效，不改任何全局配置。
    try:
        from loguru import logger as _lg

        _lg.remove()
    except Exception:
        pass

    owner_id = args.owner_id if args.owner_id is not None else settings.shared_kb_owner_id
    ks = parse_ks(args.ks)

    testset_path = Path(args.testset)
    if not testset_path.is_absolute():
        # 相对路径按项目根解析（scripts/eval_retrieval.py → 项目根）
        testset_path = default_testset_path().parent.parent.parent / testset_path
    if not testset_path.exists():
        print(f"评测集不存在: {testset_path}", file=sys.stderr)
        return 2

    questions = load_cases(testset_path, args.limit)
    if not questions:
        print("评测集为空", file=sys.stderr)
        return 2

    try:
        overrides_a = parse_overrides(args.a)
        overrides_b = parse_overrides(args.b)
    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        return 2

    if not args.cache:
        for ov in (overrides_a, overrides_b):
            ov.setdefault("use_search_cache", False)

    print(f"评测集: {testset_path.name} | 题目数: {len(questions)} | owner_id={owner_id} | ks={ks}")

    runs: list[tuple[str, dict]] = []
    label_a = args.label_a if args.label_a != "A" else f"A[{args.a or 'base'}]"
    label_b = args.label_b if args.label_b != "B" else f"B[{args.b or 'base'}]"

    print(f"\n[1/2] 运行 {label_a} ...", file=sys.stderr)
    run_a = run_config(questions, overrides_a, owner_id, ks)
    runs.append((label_a, run_a))

    if args.b is not None:
        print(f"[2/2] 运行 {label_b} ...", file=sys.stderr)
        run_b = run_config(questions, overrides_b, owner_id, ks)
        runs.append((label_b, run_b))

    report = render_report(
        f"检索质量评测报告  ({testset_path.stem}, {len(questions)} 题)",
        runs,
        ks,
    )
    print("\n" + report)

    if args.show_misses:
        for label, run in runs:
            print(f"\n--- {label} ---")
            print(render_misses(run, ks))

    if args.json_out:
        payload = {
            "testset": testset_path.stem,
            "n_questions": len(questions),
            "owner_id": owner_id,
            "ks": list(ks),
            "runs": [
                {"label": label, "metrics": run["metrics"], "per_question": run["per_question"]}
                for label, run in runs
            ],
        }
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n结果已写入: {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
