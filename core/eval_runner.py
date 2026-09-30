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

import contextlib
import contextvars
import json
import math
import re
import sys
import time
import types
from pathlib import Path
from typing import Any

from loguru import logger

from config.settings import settings
from core.db.engine import run_async_from_thread
from core.db.eval_reports import update_eval_report
from core.telemetry import span

# 判定"如实回答不知道"的关键词。知识库检索为空时，系统提示词要求回答
# "我的知识库中没有这方面的信息"，这里按同一口径检验。
_REFUSAL_MARKERS = (
    "知识库中没有",
    "知识库中未找到",
    "没有相关信息",
    "未找到相关",
    "没有这方面的信息",
    "暂无相关",
    "没有记录",
    "无法回答",
)

# 除拒绝话术之外、"不算实质内容"的措辞:道歉、建议、引导用户补充信息的话术。
# 这些词本身不构成"回答了问题"，统计残差时要剔除，否则会把一句啰嗦的
# 拒绝误判成"给了内容"。
_NON_CONTENT_MARKERS = (
    "很抱歉",
    "抱歉",
    "对不起",
    "不好意思",
    "感谢",
    "谢谢",
    "我的",
    "建议",
    "请您",
    "请你",
    "您可以",
    "你可以",
    "请先",
    "请尝试",
    "请",
    "上传",
    "提供",
    "补充",
    "告知",
    "说明",
    "联系",
    "咨询",
    "提问",
    "询问",
    "这方面",
    "相关",
    "的信息",
    "信息",
    "暂时",
    "目前",
    "当前",
)

# 去掉话术后，残留的实义字符达到多少就算"确实给了内容"。
# 取 10 是小样本权衡后的值:纯拒绝 / 啰嗦拒绝的残差通常 ≤ 9（见 _looks_like_refusal
# 的说明），而"先拒后答"的实质句子一般在 15 字以上，两者之间有充足余量。
_MIN_SUBSTANTIVE_CHARS = 10

# 归一化残差时要去掉的标点与空白
_PUNCT_RE = re.compile(r"[\s，。！？、；：,.!?;:（）()【】\[\]「」『』\-—…~·\"'“”‘’]")

# ── 默认参数（API 路由与评测 Agent 共用同一份，避免两处漂移）──
DEFAULT_TESTSET = "auto_eval"
DEFAULT_METRICS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
# 单次评测默认只跑前 N 条：全量评测很慢（每条要跑一次完整问答 + LLM 判定）
DEFAULT_SAMPLE_LIMIT = 5

# ── 检索参数覆盖（A/B 评测用）──
#
# 需求背景:评测要能"只改一个变量"做 A/B（例如同一评测集分别跑
# ``use_hybrid_search=true`` 与 ``false``，再看指标差异）。检索参数原先全走
# 全局 ``settings``，而评测任务跑在**线程池**里 —— 直接改 settings 会串味
# （同时在跑的用户请求会读到被改的值），因此**不能**改 settings。
#
# 做法:``contextvars`` + 给 ``retrieve()`` 打一层"覆盖感知"包装。retrieve
# 内部是从 ``settings`` 读默认值的（``rag/`` 不在本任务可改范围内），没法用
# 显式传参把覆盖送进去；于是退一步:包装函数从当前**上下文**读覆盖参数。
# contextvars 按线程/上下文隔离，评测线程设的值不会泄漏到别的线程，全局
# settings 也一字未动（见 tests/test_eval_reporting.py 的断言）。
#
# 局限（如实说明）:
#   1. 只有 retrieve() 形参里存在的参数能生效（下表 APPLICABLE_OVERRIDE_KEYS）。
#   2. ``retrieval_min_score`` 是 retrieve 内部直接读 settings 的、注入不进；
#      ``chunk_size`` / ``chunk_overlap`` / ``use_semantic_splitter`` 属于
#      **建库时**的分块参数，改了要重建知识库才有效 —— 这些会被记进配置快照，
#      但明确标为"未生效"（ignored_overrides），不假装起了作用。
#   3. 包装是"进程内一次性安装"的模块属性替换，本身是全局动作；但它无状态，
#      只做 contextvar 查表 + 透传，未设覆盖时行为与原来完全一致，因此线程安全。

# 请求里的 settings 风格名字 → retrieve() 的形参名
_OVERRIDE_TO_KWARG = {
    "top_k_search": "top_k_search",
    "top_k_rerank": "top_k_rerank",
    "use_hybrid_search": "use_hybrid",
    "bm25_weight": "bm25_weight",
    "use_search_cache": "use_cache",
}
# 能真正作用到本次检索的覆盖键
APPLICABLE_OVERRIDE_KEYS = frozenset(_OVERRIDE_TO_KWARG)
# 只记录、不生效的覆盖键（要么 retrieve 内部直接读 settings，要么属建库参数）
SNAPSHOT_ONLY_OVERRIDE_KEYS = frozenset(
    {"retrieval_min_score", "chunk_size", "chunk_overlap", "use_semantic_splitter"}
)

# retrieve() 的默认形参值（见 rag/retriever.py 签名）。settings 里没有这几项，
# 所以"本次真正生效的值"在没有覆盖时就是这几个默认值。
_RETRIEVE_DEFAULTS = {"top_k_search": 10, "top_k_rerank": 5, "bm25_weight": 0.3}

# 逐题判"答对"时 faithfulness 的及格线（与诊断建议里的阈值一致）
_FAITHFULNESS_PASS = 0.8

# 当前上下文里的检索覆盖参数；None = 未设覆盖，走默认/配置
_RETRIEVE_OVERRIDES: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "eval_retrieve_overrides", default=None
)


def _make_override_aware(retrieve_fn):
    """
    把 retrieve() 包成"覆盖感知"版本:调用时把当前上下文的覆盖并入关键字参数。

    覆盖**优先于**调用方显式传入的值（agent 里写死 ``top_k_rerank=5``，但 A/B
    里我们要能把它改成 10），也优先于 settings 默认值。
    """

    def wrapper(*args, **kwargs):
        overrides = _RETRIEVE_OVERRIDES.get()
        if overrides:
            for key, value in overrides.items():
                kwarg = _OVERRIDE_TO_KWARG.get(key)
                if kwarg is not None:
                    kwargs[kwarg] = value
        return retrieve_fn(*args, **kwargs)

    wrapper.__name__ = getattr(retrieve_fn, "__name__", "retrieve")
    wrapper._eval_override_aware = True  # type: ignore[attr-defined]
    return wrapper


def _install_retrieve_override_hook() -> None:
    """
    安装检索覆盖钩子（幂等）。

    agent 模块用 ``from rag.retriever import retrieve`` 在**导入时**绑定了原函数，
    只改 ``rag.retriever.retrieve`` 不会影响它们，必须把 agent 里那两个名字一并换成
    包装 —— 否则覆盖"看起来生效了，实际没进检索"。
    """
    import rag.retriever as retriever

    current = retriever.retrieve
    if getattr(current, "_eval_override_aware", False):
        return
    wrapped = _make_override_aware(current)
    retriever.retrieve = wrapped

    import agent.chat_agent as chat_agent
    import agent.tools as tools

    chat_agent.retrieve = wrapped
    tools.retrieve = wrapped


@contextlib.contextmanager
def applied_retrieve_overrides(overrides: dict[str, Any] | None):
    """
    在上下文里临时启用一组检索覆盖参数。

    进入时安装钩子并把覆盖写进 contextvar；退出时**务必重置**（token reset）——
    线程池会复用线程，不重置会把覆盖泄漏给排在后面的评测任务。
    """
    _install_retrieve_override_hook()
    token = _RETRIEVE_OVERRIDES.set(overrides or None)
    try:
        yield
    finally:
        _RETRIEVE_OVERRIDES.reset(token)


def validate_retrieve_overrides(overrides: dict[str, Any] | None) -> dict[str, Any]:
    """校验覆盖键；未知键（多半是拼错）抛 ValueError，让路由层 400 报错。"""
    if not overrides:
        return {}
    unknown = set(overrides) - APPLICABLE_OVERRIDE_KEYS - SNAPSHOT_ONLY_OVERRIDE_KEYS
    if unknown:
        raise ValueError(f"未知的检索覆盖字段: {', '.join(sorted(unknown))}")
    return dict(overrides)


def build_config_snapshot(
    testset: str,
    metrics: list[str],
    sample_limit: int | None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    构造本次评测的**配置快照** —— 记录"这次到底用的是哪套检索配置"。

    基线 = 全局 settings + retrieve 默认值；再叠加本次覆盖得到"真正生效"的
    检索配置。同时把"请求了但没生效"的字段单列（ignored_overrides），
    避免快照把没起作用的东西记成已生效。
    """
    overrides = overrides or {}
    base: dict[str, Any] = {
        "top_k_search": _RETRIEVE_DEFAULTS["top_k_search"],
        "top_k_rerank": _RETRIEVE_DEFAULTS["top_k_rerank"],
        "bm25_weight": _RETRIEVE_DEFAULTS["bm25_weight"],
        "use_hybrid_search": settings.use_hybrid_search,
        "use_search_cache": settings.use_search_cache,
        "retrieval_min_score": settings.retrieval_min_score,
        "use_semantic_splitter": settings.use_semantic_splitter,
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "model": settings.deepseek_model,
    }
    effective = dict(base)
    applied: dict[str, Any] = {}
    for key, value in overrides.items():
        if key in APPLICABLE_OVERRIDE_KEYS:
            effective[key] = value
            applied[key] = value
    ignored = {k: v for k, v in overrides.items() if k not in APPLICABLE_OVERRIDE_KEYS}
    return {
        **effective,
        "testset": testset,
        "sample_limit": sample_limit,
        "metrics": list(metrics),
        "retrieve_overrides": dict(overrides),
        "applied_overrides": applied,
        "ignored_overrides": ignored,
    }


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


def _forward_stdlib_logs_to_loguru() -> None:
    """
    把 ragas 的 stdlib 日志接到 loguru 上。

    **为什么必须做这一步**：ragas 用标准库 ``logging`` 报错，而本项目用 loguru ——
    loguru 不接管 stdlib 的 root logger，于是 ragas 的报错**直接进了黑洞**。
    后果是"评测跑完了、指标全是空的、日志里一个字都没有"，只能靠猜。

    实测那两个吃掉整列指标的失败就是这么被吞掉的：

        ERROR ragas.executor: Job[4]: LLMDidNotFinishException(
            The LLM generation was not completed. Please increase the max_tokens...)
        ERROR ragas.executor: Job[1]: OpenAIInvalidRequestError(400 -
            Invalid n value (currently only n = 1 is supported))

    接上之后，同样的失败会出现在应用日志里，一眼能看出原因。
    """
    import logging

    if getattr(_forward_stdlib_logs_to_loguru, "_installed", False):
        return

    class _ToLoguru(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            logger.opt(depth=6, exception=record.exc_info).log(level, record.getMessage())

    handler = _ToLoguru()
    for name in ("ragas", "ragas.executor", "ragas.metrics", "instructor"):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        # 别把 ragas 的 INFO 灌进来，只看它出问题时的 WARNING/ERROR
        lg.setLevel(logging.WARNING)
        lg.propagate = False

    _forward_stdlib_logs_to_loguru._installed = True


def _load_ragas():
    """延迟导入 ragas（它依赖较重，且需要先装垫片）。"""
    _install_ragas_compat_shim()
    _forward_stdlib_logs_to_loguru()
    try:
        from ragas import EvaluationDataset, RunConfig, evaluate
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.llms import LangchainLLMWrapper
        from ragas.metrics import (
            answer_relevancy,
            context_precision,
            context_recall,
            faithfulness,
        )
    except ImportError as e:
        # ragas 被拆到 requirements-eval.txt：它的依赖（instructor → jiter<0.15）
        # 与主依赖树的 openai（→ jiter>=0.16）互斥，混在一起会让整份
        # requirements.txt 都装不上。所以评测是可选功能，缺了就明确说出来。
        raise RuntimeError(
            "评测依赖未安装（ragas）。它不在主依赖里，因为与 langchain/openai "
            "存在无法调和的版本冲突。需要评测功能时单独安装："
            "pip install -r requirements-eval.txt"
        ) from e

    return {
        "EvaluationDataset": EvaluationDataset,
        "RunConfig": RunConfig,
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


def _build_run_config(rag) -> Any:
    """
    裁判调用的并发与超时。

    为什么必须显式构造：ragas 默认 `max_workers=16` / `timeout=180s`，对第三方
    DeepSeek 代理太激进。实测**服务器上 18 个裁判任务全部 TimeoutError**，而同一批
    数据在本地全过 —— 差别就是"16 路并发、每路还是 32768 的大预算"打到代理上排队。
    降到 4 路并发 + 600 秒超时：代理压力小、单次更快，慢调用也有余量。

    这一条是靠 `_forward_stdlib_logs_to_loguru()` 才看见的 —— 在此之前这些
    TimeoutError 只会变成几列 NaN，日志里一个字都没有。
    """
    from config.settings import settings

    return rag["RunConfig"](
        max_workers=settings.eval_judge_concurrency,
        timeout=settings.eval_judge_timeout_seconds,
        max_retries=settings.eval_judge_max_retries,
    )


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
            out.append(
                {
                    "name": path.stem,
                    "title": data.get("name", path.stem),
                    "count": len(questions),
                }
            )
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
        d.get("content", "") for d in (final_state.get("retrieved_docs") or []) if d.get("content")
    ]
    steps = [s for s in (final_state.get("reasoning_log") or []) if isinstance(s, str)]
    return answer, contexts, steps


def _has_substantive_content(answer: str) -> bool:
    """
    去掉拒绝话术与"非内容"措辞后，判断还剩下多少实义字符。

    返回 True 表示"除了说不知道，还额外讲了实质内容"（即先拒后答）。
    """
    residual = answer
    for marker in _REFUSAL_MARKERS:
        residual = residual.replace(marker, "")
    for marker in _NON_CONTENT_MARKERS:
        residual = residual.replace(marker, "")
    residual = _PUNCT_RE.sub("", residual)
    return len(residual) >= _MIN_SUBSTANTIVE_CHARS


def _looks_like_refusal(answer: str) -> bool:
    """
    回答是否属于"如实说不知道"。

    旧实现是**裸子串匹配**:只要出现拒绝话术就算拒答。它会被"先拒后答"骗过 ——
    "知识库中没有这方面的信息。不过据我所知，他曾在字节跳动做算法工程师。"
    命中了"知识库中没有"，于是编造内容被判成**诚实拒答**，honesty_rate 虚高、
    幻觉被掩盖。这比崩溃更危险（产出一个看似合理、实则错的指标）。

    新判据:**既要命中拒绝话术，又要"没剩下实质内容"**。

    召回/精确权衡:
      - 精确（不把幻觉误判成拒答）优先 —— 这是 honesty_rate 的意义所在。
      - 代价是"很啰嗦的拒绝"若在话术之外还残留 ≥ _MIN_SUBSTANTIVE_CHARS 个
        实义字符（例如多讲了一句具体建议），会被判成非拒答（漏判）。我们用
        一份"非内容措辞"白名单（道歉/建议/引导用户补充信息）压低这种误伤。
      - 更稳的做法是让判定 LLM 输出结构化标签（{refused: bool, reason}），
        精确度和召回都更好；但那要多一次 LLM 调用、依赖联网、且无法离线测试，
        故此处选用可离线、可断言的启发式。
    """
    text = (answer or "").strip()
    if not text:
        return False
    if not any(marker in text for marker in _REFUSAL_MARKERS):
        return False
    return not _has_substantive_content(text)


def _as_number(value: Any) -> float | None:
    """
    把 RAGAS 结果里的单个值转成数字;NaN / 非数字（含 bool）返回 None。

    为什么必须挡 NaN:pandas 对"该题指标算不出来"会填 NaN，而 ``isinstance(NaN,
    float)`` 为真 —— 旧实现直接把它当数字参与平均，一个 NaN 就把整条指标污染成
    NaN。这里显式剔除，只让真实数值进入聚合。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return float(value)


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


def run_eval_task(
    report_id: int,
    owner_id: int,
    testset_name: str,
    metrics: list[str],
    limit: int | None = None,
    retrieve_overrides: dict[str, Any] | None = None,
) -> None:
    """
    执行一次评测并落库。**同步函数**，由线程池调度。

    评测很慢（每条问题要跑一次完整问答图，RAGAS 判定又要额外调多次 LLM），
    因此必须作为后台任务，且默认只跑前 N 条。

    ``retrieve_overrides``:本次评测专用的检索参数覆盖（A/B 用）。它经
    ``contextvars`` 注入检索链路，**不改全局 settings**，因此不会影响同时
    在跑的其它请求。真正生效的配置会连同基线一起写进报告的快照
    （``config_json``），使两次评测的差异可直接 diff。

    报告状态语义（前端据此判断报告可信度）:

    - ``done``    —— 请求的 RAGAS 指标**全部**算出，且没有题目因检索为空被排除。
                    报告完整可信，可以直接拿去做对比。
    - ``partial`` —— 跑完了、结果可用，但有已知降级:某个 RAGAS 指标没算出来
                    （``rag_error`` 非空但仍有其它指标），或有题目因检索为空被
                    排除（这些题没进指标聚合，会拉低有效样本量）。``error``
                    字段写明原因。
    - ``failed``  —— 没有任何可用指标产出（RAGAS 抛异常或未产出指标 —— 再也
                    **不会**像旧实现那样照样报 ``done``），或任务整体异常、
                    评测集为空。

    ``pending`` / ``running`` 是提交后、开跑前的中间态，含义不变。
    """
    started = time.time()
    retrieve_overrides = validate_retrieve_overrides(retrieve_overrides) or None
    try:
        run_async_from_thread(update_eval_report(report_id, status="running", progress=5))

        questions = load_testset(testset_name)
        questions = _select_samples(questions, limit)
        if not questions:
            run_async_from_thread(
                update_eval_report(
                    report_id,
                    status="failed",
                    error="评测集为空",
                    progress=100,
                    config_json=json.dumps(
                        build_config_snapshot(testset_name, metrics, limit, retrieve_overrides),
                        ensure_ascii=False,
                    ),
                )
            )
            return

        # 配置快照尽早写库:即使后面整段跑挂，也能看出"这次用的哪套配置"。
        config_snapshot = build_config_snapshot(testset_name, metrics, limit, retrieve_overrides)
        run_async_from_thread(
            update_eval_report(
                report_id,
                total_questions=len(questions),
                progress=10,
                config_json=json.dumps(config_snapshot, ensure_ascii=False),
            )
        )

        # ── 1. 逐题生成回答 ──
        # 整段包在 applied_retrieve_overrides 里:contextvar 只在**本线程/本上下文**
        # 生效，检索链路（含 agent 里 asyncio.to_thread 派生的子线程）都能读到，
        # 但不会污染全局 settings，也不会串到别的评测任务或用户请求上。
        samples: list[dict] = []
        sample_qidx: list[int] = []  # samples[i] 对应 per_question[sample_qidx[i]]
        per_question: list[dict] = []
        retrieval_failed_count = 0
        with applied_retrieve_overrides(retrieve_overrides):
            for i, item in enumerate(questions, start=1):
                question = item.get("question", "")
                expected = item.get("expected_answer", "") or ""
                category = item.get("category", "")
                try:
                    with span("eval.question", category=category, qid=str(item.get("id", ""))):
                        answer, contexts, _steps = _answer_one_sync(question, owner_id)
                except Exception as e:
                    logger.error("[Eval] 生成回答失败: {} - {}", question[:40], e)
                    answer, contexts = "", []

                # 检索为空 = 这道题**没有可用于判定的上下文**。标记它，并把它排除
                # 出 RAGAS 聚合 —— 旧实现给它塞占位串 "（未检索到任何上下文）"，
                # 占位串会被当成真实上下文去算 faithfulness/relevancy，产出一个
                # 看似合理、实则无意义的分数（比崩溃更糟）。这里改为"标记 + 排除"。
                retrieval_failed = len(contexts) == 0
                if retrieval_failed:
                    retrieval_failed_count += 1
                    logger.info("[Eval] 检索为空，已排除出指标聚合: {}", question[:40])

                refused = _looks_like_refusal(answer)
                qidx = len(per_question)
                per_question.append(
                    {
                        "id": item.get("id"),
                        "category": category,
                        "question": question,
                        "expected_answer": expected,
                        "answer": answer,
                        "contexts_count": len(contexts),
                        "retrieval_failed": retrieval_failed,
                        "refused": refused,
                    }
                )
                if not retrieval_failed:
                    samples.append(
                        {
                            "user_input": question,
                            "response": answer or "（无回答）",
                            "retrieved_contexts": contexts,
                            "reference": expected or "（无期望答案）",
                        }
                    )
                    sample_qidx.append(qidx)

                progress = 10 + int(45 * i / len(questions))
                run_async_from_thread(update_eval_report(report_id, progress=progress))
                logger.info("[Eval] 已生成 {}/{} : {}", i, len(questions), question[:40])

        # ── 2. RAGAS 指标 ──
        metric_scores: dict[str, Any] = {}
        rag_error: str | None = None
        # 覆盖率不足的指标（"算出了 n / 共 m 条"）。要在 try 外面用 ——
        # 状态判定必须因为它降级，否则"5 题只有 1 题算出分"会被报成干净的 done。
        coverage_gaps: list[str] = []
        try:
            if not samples:
                rag_error = "所有题目的检索结果均为空，没有可判定的样本，RAGAS 指标未计算"
            else:
                rag = _load_ragas()
                from config.context import get_context
                from config.settings import settings as _settings

                # 裁判模型必须单独给一块**更大**的 token 预算，理由见
                # settings.llm_judge_max_tokens：ragas 要裁判输出"全部断言 + 逐条
                # 判定"的长 JSON，用回答问题的 8192 会被思考挤爆 → ragas 抛
                # LLMDidNotFinishException → 该样本静默变 NaN。
                judge_llm = rag["LangchainLLMWrapper"](
                    get_context().chat.chat_model(
                        temperature=0.0,
                        streaming=False,
                        max_tokens=_settings.llm_judge_max_tokens,
                    )
                )
                judge_emb = rag["LangchainEmbeddingsWrapper"](get_context().embed.embeddings())

                # answer_relevancy 默认 strictness=3 —— 它要裁判对同一输入做 n=3 次
                # 生成再取平均。而本项目走第三方 DeepSeek 代理，**只支持 n=1**，
                # 传 3 直接 400（实测：5 道题全灭）。降到 1；代价是少一点稳定性，
                # 总比整列 NaN 好。
                # hasattr 判断是为了容错：测试会注入没有 strictness 的假指标对象。
                _relevancy = rag["metrics"].get("answer_relevancy")
                if _relevancy is not None and hasattr(_relevancy, "strictness"):
                    _relevancy.strictness = 1

                selected = [rag["metrics"][m] for m in metrics if m in rag["metrics"]]
                if not selected:
                    rag_error = "未选择任何有效的 RAGAS 指标"
                else:
                    dataset = rag["EvaluationDataset"].from_list(samples)
                    result = rag["evaluate"](
                        dataset=dataset,
                        metrics=selected,
                        llm=judge_llm,
                        embeddings=judge_emb,
                        run_config=_build_run_config(rag),
                    )
                    # ragas 返回的结果对象可转 dict；只保留数值型指标
                    raw = (
                        result.to_pandas().to_dict(orient="list")
                        if hasattr(result, "to_pandas")
                        else dict(result)
                    )
                    per_metric_rows: dict[str, list] = {}
                    # 每个指标"实际算出了几条 / 总共几条"。ragas 把失败样本记成 NaN，
                    # 聚合时又被剔除 —— 于是平均值可能只覆盖一小部分样本。
                    # **必须把它显式记下来**，否则"5 题里只有 1 题算出分"的那个平均值
                    # 会被当成整体指标展示（实测踩过：0.1951 其实是 1 个样本的平均）。
                    metric_coverage: dict[str, tuple[int, int]] = {}
                    for key, value in raw.items():
                        if key in ("user_input", "response", "retrieved_contexts", "reference"):
                            continue
                        try:
                            rows = list(value)
                        except TypeError:
                            continue
                        nums = [_as_number(v) for v in rows]
                        vals = [v for v in nums if v is not None]
                        metric_coverage[key] = (len(vals), len(nums))
                        if vals:
                            metric_scores[key] = round(sum(vals) / len(vals), 4)
                            per_metric_rows[key] = nums
                    # 把逐题分数挂回 per_question（compare 接口按题对比要用它）
                    for pos, qidx in enumerate(sample_qidx):
                        for key, nums in per_metric_rows.items():
                            if pos < len(nums) and nums[pos] is not None:
                                per_question[qidx][key] = round(nums[pos], 4)
                    # 覆盖率如实记下来（键以下划线开头，前端不会把它当指标渲染），
                    # 并且**只要有指标没覆盖全部样本，就把状态压成 partial 并写进原因**。
                    # 否则会出现"状态 done、其实是 1/5 个样本的平均值"这种最坏情况。
                    metric_scores["_coverage"] = {
                        k: f"{n}/{m}" for k, (n, m) in metric_coverage.items()
                    }
                    coverage_gaps = [
                        f"{k} 只算出 {n}/{m} 个样本"
                        for k, (n, m) in metric_coverage.items()
                        if m and n < m
                    ]
                    if coverage_gaps:
                        note = "部分样本未算出（裁判调用失败）：" + "；".join(coverage_gaps)
                        rag_error = f"{rag_error}；{note}" if rag_error else note

                    if not any(k for k in metric_scores if not k.startswith("_")):
                        # 注意上面刚塞进去的 _coverage 会让 dict 非空，所以这里
                        # 判断的是"有没有真指标"，不是"dict 空不空"
                        rag_error = "RAGAS 未产出任何指标"
        except Exception as e:
            logger.error("[Eval] RAGAS 指标计算失败: {}", e)
            rag_error = f"RAGAS 指标计算失败: {str(e)[:300]}"

        run_async_from_thread(update_eval_report(report_id, progress=85))

        # ── 3. 自建指标：诚实度 ──
        # "幻觉检测" 类问题 = 知识库中本就没有答案，正确行为是如实说不知道。
        # 注意:诚实度**不**按"检索是否为空"排除题目 —— 它衡量的是行为（该不该
        # 拒答），与有没有检索到上下文无关；知识库外问题本来就不该检索到东西。
        hallucination_items = [q for q in per_question if "幻觉" in (q["category"] or "")]
        if hallucination_items:
            honest = sum(1 for q in hallucination_items if q["refused"])
            honesty_rate = round(honest / len(hallucination_items), 4)
            hallucination_count = len(hallucination_items) - honest
        else:
            honesty_rate = None
            hallucination_count = 0

        answered = [q for q in per_question if q["answer"]]
        avg_ctx = round(sum(q["contexts_count"] for q in per_question) / len(per_question), 2)

        # ── 4. 状态判定（语义见 docstring）──
        # 旧实现无论 RAGAS 成功与否都报 done:一次"成功"的评测可能一个新指标都
        # 没有（错误只塞进 metric_scores["_error"]），调用方据此以为拿到了结果。
        # 现在按"请求的指标是否真的产出、有没有题被排除"如实分档。
        requested_count = len(metrics)
        produced_count = sum(1 for m in metrics if isinstance(metric_scores.get(m), (int, float)))
        excluded = retrieval_failed_count
        if produced_count == 0:
            status = "failed"
        elif produced_count < requested_count or excluded > 0 or coverage_gaps:
            # coverage_gaps：某个指标只覆盖了一部分样本（其余样本裁判调用失败、
            # 被 ragas 记成 NaN）。平均值的样本量不足，不能算干净的 done。
            status = "partial"
        else:
            status = "done"

        reasons: list[str] = []
        if rag_error:
            reasons.append(rag_error)
        if excluded:
            reasons.append(f"{excluded} 题因检索为空被排除，未计入 RAGAS 指标聚合")
        status_error = "；".join(reasons) if status != "done" else ""

        # ── 5. 诊断建议 ──
        recommendations: list[str] = []
        if metric_scores.get("faithfulness") is not None and metric_scores["faithfulness"] < 0.8:
            recommendations.append(
                "faithfulness 偏低：回答与检索内容的吻合度不足，"
                "检查系统提示词的防幻觉约束或降低 temperature"
            )
        if (
            metric_scores.get("context_recall") is not None
            and metric_scores["context_recall"] < 0.7
        ):
            recommendations.append(
                "context_recall 偏低：期望答案的信息没被检索到，"
                "考虑调大 top_k、开启混合检索或改进分块策略"
            )
        if (
            metric_scores.get("context_precision") is not None
            and metric_scores["context_precision"] < 0.7
        ):
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
        run_async_from_thread(
            update_eval_report(
                report_id,
                status=status,
                progress=100,
                completed=len(per_question),
                results_json=json.dumps(per_question, ensure_ascii=False),
                recommendations_json=json.dumps(recommendations, ensure_ascii=False),
                metrics_json=json.dumps(metric_scores, ensure_ascii=False),
                config_json=json.dumps(config_snapshot, ensure_ascii=False),
                honesty_rate=honesty_rate,
                hallucination_count=hallucination_count,
                avg_match_score=metric_scores.get("faithfulness"),
                # poor_retrieval_count = 因检索为空被排除、未计入指标聚合的题数
                poor_retrieval_count=retrieval_failed_count,
                answered_count=len(answered),
                duration_seconds=duration,
                error=status_error,
            )
        )
        logger.info(
            "[Eval] 评测{} report={}: {} 题（排除 {} 题）, 指标={}, 状态={}, 耗时 {}s",
            "完成" if status == "done" else "结束（降级）",
            report_id,
            len(per_question),
            retrieval_failed_count,
            metric_scores,
            status,
            duration,
        )

    except Exception as e:
        logger.error("[Eval] 评测任务失败: {}", e)
        try:
            run_async_from_thread(
                update_eval_report(
                    report_id,
                    status="failed",
                    progress=100,
                    error=str(e)[:500],
                )
            )
        except Exception as inner:
            logger.error("[Eval] 标记评测失败也失败了: {}", inner)


# ------------------------------------------------------------
# A/B 对比（GET /api/eval/compare 的纯逻辑，可离线单测）
# ------------------------------------------------------------


def _load_json_field(value: Any, default: Any) -> Any:
    """把库里的 JSON-in-TEXT 字段解析出来;空/坏值统一回退到 default。"""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# 配置差异里不逐字段展开的结构性字段（它们的内容已在 a.config / b.config 里，
# 展开成 "字段: {a: {...}, b: {...}}" 只会让前端表难渲染）
_CONFIG_DIFF_SKIP = frozenset(
    {"retrieve_overrides", "applied_overrides", "ignored_overrides", "metrics"}
)


def _config_diff(ca: dict, cb: dict) -> dict:
    """逐字段对比两份配置快照，只返回**有差异**的标量字段（便于表格展示）。"""
    diff: dict[str, Any] = {}
    for key in sorted(set(ca) | set(cb)):
        if key in _CONFIG_DIFF_SKIP:
            continue
        va, vb = ca.get(key), cb.get(key)
        if va != vb:
            diff[key] = {"a": va, "b": vb}
    return diff


def _metric_diff(ma: dict, mb: dict) -> dict:
    """逐指标对比 A/B，给出 delta = B - A（A 为基线）。"""
    out: dict[str, Any] = {}
    for key in sorted(set(ma) | set(mb)):
        if key.startswith("_"):  # 旧的错误占位键，不是指标
            continue
        va, vb = ma.get(key), mb.get(key)
        delta = None
        if (
            isinstance(va, (int, float))
            and isinstance(vb, (int, float))
            and not isinstance(va, bool)
            and not isinstance(vb, bool)
        ):
            delta = round(vb - va, 4)
        out[key] = {"a": va, "b": vb, "delta": delta}
    return out


def _question_correct(q: dict) -> bool | None:
    """
    单题"答对"的判定规则（逐题对比用，规则刻意保持简单、可解释）:

    - 检索为空 → ``None``:没有上下文无从判定，不计入胜负。
    - "幻觉检测"类（知识库外问题）→ 正确 = **如实拒答**。
    - 其余题目 → 有逐题 ``faithfulness`` 就以 ``>= _FAITHFULNESS_PASS`` 为准;
      没有（RAGAS 失败）则退回"给了回答且没拒答"。
    """
    if q.get("retrieval_failed"):
        return None
    if "幻觉" in (q.get("category") or ""):
        return bool(q.get("refused"))
    faith = q.get("faithfulness")
    if isinstance(faith, (int, float)) and not (isinstance(faith, float) and math.isnan(faith)):
        return faith >= _FAITHFULNESS_PASS
    return bool(q.get("answer")) and not q.get("refused")


def _index_questions(questions: list[dict]) -> dict:
    """按 id 建索引（无 id 时退化为用问题文本），用于逐题对齐。"""
    out: dict[Any, dict] = {}
    for q in questions:
        key = q.get("id")
        if key is None:
            key = q.get("question", "")
        out[key] = q
    return out


def _question_entry(qa_: dict, qb_: dict, correct_a: bool | None, correct_b: bool | None) -> dict:
    return {
        "id": qa_.get("id", qb_.get("id")),
        "category": qa_.get("category") or qb_.get("category"),
        "question": qa_.get("question") or qb_.get("question"),
        "a": {
            "correct": correct_a,
            "refused": bool(qa_.get("refused")),
            "retrieval_failed": bool(qa_.get("retrieval_failed")),
            "faithfulness": qa_.get("faithfulness"),
        },
        "b": {
            "correct": correct_b,
            "refused": bool(qb_.get("refused")),
            "retrieval_failed": bool(qb_.get("retrieval_failed")),
            "faithfulness": qb_.get("faithfulness"),
        },
    }


def _report_summary(report: dict, config: dict, count: int) -> dict:
    return {
        "report_id": report.get("id"),
        "status": report.get("status"),
        "testset": report.get("testset_name"),
        "sample_count": count,
        "created_at": str(report.get("created_at", "")),
        "config": config,
    }


def build_report_comparison(a: dict, b: dict) -> dict:
    """
    对比两次评测报告（A/B）。入参是 ``get_eval_report`` 返回的 dict。

    返回结构:

    - ``comparable`` / ``comparability_notes``:样本数或评测集不同 → 明确"不可
      直接比较"（comparable=False）并给出原因;状态非 done/partial 只算警告。
    - ``config_diff``:两次配置快照的差异字段（A/B 值）。
    - ``metrics``:各指标的 A/B 值与 delta = B - A。
    - ``per_question``:逐题差异 —— A 对 B 错、B 对 A 错、拒答变化、检索变化。
    """
    ca = _load_json_field(a.get("config_json"), {})
    cb = _load_json_field(b.get("config_json"), {})
    ma = _load_json_field(a.get("metrics_json"), {})
    mb = _load_json_field(b.get("metrics_json"), {})
    qa = _load_json_field(a.get("results_json"), [])
    qb = _load_json_field(b.get("results_json"), [])

    count_a = int(a.get("total_questions") or 0)
    count_b = int(b.get("total_questions") or 0)

    blockers: list[str] = []
    warnings: list[str] = []
    if count_a != count_b:
        blockers.append(f"两次样本数不同（A={count_a}, B={count_b}），指标不可直接比较")
    if ca.get("testset") != cb.get("testset"):
        blockers.append(
            f"两次评测集不同（A={ca.get('testset')}, B={cb.get('testset')}），不可直接比较"
        )
    for label, report in (("A", a), ("B", b)):
        if report.get("status") not in ("done", "partial"):
            warnings.append(f"{label} 状态为 {report.get('status')}（非成功完成），结果可能不完整")

    ia, ib = _index_questions(qa), _index_questions(qb)
    common = [k for k in ia if k in ib]
    a_correct_b_wrong: list[dict] = []
    b_correct_a_wrong: list[dict] = []
    refusal_changed: list[dict] = []
    retrieval_changed: list[dict] = []
    for key in common:
        qa_, qb_ = ia[key], ib[key]
        correct_a, correct_b = _question_correct(qa_), _question_correct(qb_)
        entry = None
        if correct_a is True and correct_b is False:
            entry = _question_entry(qa_, qb_, correct_a, correct_b)
            a_correct_b_wrong.append(entry)
        elif correct_b is True and correct_a is False:
            entry = _question_entry(qa_, qb_, correct_a, correct_b)
            b_correct_a_wrong.append(entry)
        if bool(qa_.get("refused")) != bool(qb_.get("refused")):
            refusal_changed.append(_question_entry(qa_, qb_, correct_a, correct_b))
        if bool(qa_.get("retrieval_failed")) != bool(qb_.get("retrieval_failed")):
            retrieval_changed.append(_question_entry(qa_, qb_, correct_a, correct_b))

    return {
        "comparable": not blockers,
        "comparability_notes": blockers + warnings,
        "a": _report_summary(a, ca, count_a),
        "b": _report_summary(b, cb, count_b),
        "config_diff": _config_diff(ca, cb),
        "metrics": _metric_diff(ma, mb),
        "per_question": {
            "common_count": len(common),
            "only_in_a": sorted(str(k) for k in ia if k not in ib),
            "only_in_b": sorted(str(k) for k in ib if k not in ia),
            "a_correct_b_wrong": a_correct_b_wrong,
            "b_correct_a_wrong": b_correct_a_wrong,
            "refusal_changed": refusal_changed,
            "retrieval_changed": retrieval_changed,
        },
    }
