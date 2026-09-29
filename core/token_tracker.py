"""
Token 使用统计与费用估算
跟踪每次 LLM 调用的 token 消耗，支持按用户、按天、按模型聚合统计
"""

import json
from datetime import timedelta

from loguru import logger
from sqlalchemy import func, select

from config.settings import settings
from core.database import TokenUsage, User, session_scope, utcnow

# 内置费率表（每 **100 万 token 的人民币价**）—— **只是兜底**。
#
# 真实费率应当通过 `settings.llm_cost_rates` 配置：不同渠道 / 代理的定价
# 各不相同（本项目走的是第三方 DeepSeek 代理），把某一家的价格写死在代码里
# 必然与实际账单不符。
#
# 原状：表里只有 `deepseek-v4-pro` / `deepseek-chat`，而实际配置的模型是
# `deepseek-flash` —— 于是**每一次调用都命中"未知模型"、成本记 0**，
# 管理页显示的费用全部来自更早的历史记录。token 在记，钱没在算。
#
# 原则不变：查不到费率的模型**不编造数字**，记 0 并告警；同时把"有哪些模型
# 没匹配上"暴露给统计接口，让界面能显示"成本未知"而不是一个会让人误读的 $0。
_BUILTIN_COST_RATES: dict[str, dict[str, float]] = {
    "deepseek-v4-pro": {"input": 0.28, "output": 1.10},
    "deepseek-chat": {"input": 0.28, "output": 1.10},
    "qwen-turbo": {"input": 0.50, "output": 0.50},
    "glm-4": {"input": 0.50, "output": 0.50},
}


def _load_cost_rates() -> dict[str, dict[str, float]]:
    """
    内置费率 ⊕ `settings.llm_cost_rates` 的覆盖。

    在导入时算一次（配置是启动时快照，与项目其它配置一致）。
    """
    rates = {m: dict(r) for m, r in _BUILTIN_COST_RATES.items()}
    raw = (getattr(settings, "llm_cost_rates", "") or "").strip()
    if not raw:
        return rates
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("settings.llm_cost_rates 不是合法 JSON，已忽略: {}", e)
        return rates

    for model, rate in (overrides or {}).items():
        if isinstance(rate, dict) and "input" in rate and "output" in rate:
            entry = {"input": float(rate["input"]), "output": float(rate["output"])}
            # cached_input 是可选的，别漏掉 —— 漏了就等于"缓存命中不省钱"
            if rate.get("cached_input") is not None:
                entry["cached_input"] = float(rate["cached_input"])
            rates[model] = entry
        else:
            logger.warning("llm_cost_rates 里 {} 的格式不对（需 input/output），已跳过", model)
    return rates


COST_RATES = _load_cost_rates()

# 每个未知模型只告警一次，避免刷日志
_warned_unknown_models: set[str] = set()
# 未匹配到费率的模型 —— 由 get_usage_stats 暴露出去，界面据此提示"成本未知"
_unmapped_models: set[str] = set()


def estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数量。
    中文字符约 1 token/1.5 字符，英文字符约 1 token/4 字符。

    仅在拿不到真实 usage 时作为兜底；正常路径应使用 provider 返回的
    ``usage_metadata``（见 core/llm.py 的 extract_usage）。
    """
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if "一" <= c <= "鿿")
    other_chars = len(text) - chinese_chars
    estimated = int(chinese_chars * 1.5 + other_chars / 4)
    return max(estimated, 1)


def calculate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
) -> float:
    """
    根据模型费率计算单次调用费用（**人民币**，与 DeepSeek 官方计价单位一致）。

    费率表中没有该模型时返回 0 并首次告警，不套用默认费率 ——
    宁可显示"成本未知"，也不要显示一个编造的数字。**未匹配的模型会被记进
    `_unmapped_models`**，由 `get_usage_stats` 暴露出去。

    Args:
        cached_tokens: 命中 prompt 缓存的输入 token 数。**缓存命中的输入更便宜**，
            所以从全价的输入里扣掉这部分。若费率里给了 `cached_input`
            就按它计价，否则这部分按 0 计（宁可低估，也不假装知道缓存价）。
    """
    rates = COST_RATES.get(model)
    if rates is None:
        _unmapped_models.add(model)
        if model not in _warned_unknown_models:
            _warned_unknown_models.add(model)
            logger.warning(
                "模型 {} 不在费率表中，成本记为 0（不会套用默认价）。"
                "如需成本统计，请在 config/.env 里设 LLM_COST_RATES，"
                '例如 {{"{}": {{"input": 0.28, "output": 1.10}}}}',
                model,
                model,
            )
        return 0.0

    # 缓存命中的输入不计全价
    cached = max(0, min(int(cached_tokens or 0), int(prompt_tokens)))
    full_price_input = max(0, int(prompt_tokens) - cached)
    cached_rate = rates.get("cached_input")

    input_cost = (full_price_input / 1_000_000) * rates["input"]
    if cached and cached_rate is not None:
        input_cost += (cached / 1_000_000) * float(cached_rate)
    output_cost = (completion_tokens / 1_000_000) * rates["output"]
    return round(input_cost + output_cost, 8)


async def track_usage(
    owner_id: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int = 0,
    cached_tokens: int = 0,
) -> None:
    """
    记录一次 token 使用。

    Args:
        prompt_tokens:     输入 token（含命中 prompt 缓存的部分）。
        completion_tokens: 输出 token。**已包含** reasoning_tokens ——
                           这是 provider 的计费口径。
        reasoning_tokens:  其中用于思考的 token 数，单独留档便于分析。
        cached_tokens:     命中 prompt 缓存的输入 token 数。
    """
    cost = calculate_cost(model, prompt_tokens, completion_tokens, cached_tokens=cached_tokens)
    try:
        async with session_scope() as session:
            session.add(
                TokenUsage(
                    owner_id=owner_id,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    reasoning_tokens=reasoning_tokens,
                    cached_tokens=cached_tokens,
                    cost_estimate=cost,
                )
            )
        logger.debug(
            "Token usage tracked: owner={}, model={}, prompt={}, completion={} "
            "(reasoning={}, cached={}), cost=${:.6f}",
            owner_id,
            model,
            prompt_tokens,
            completion_tokens,
            reasoning_tokens,
            cached_tokens,
            cost,
        )
    except Exception as e:
        logger.error(f"Failed to track token usage: {e}")


async def get_usage_stats(owner_id: int | None, days: int = 7) -> dict:
    """
    获取 token 使用统计：每日分解、模型分解、总计。

    Args:
        owner_id: ``None`` 表示**全部用户**（管理员视角）。给具体 ID 则只统计该用户。
            传 None 时会额外返回 ``by_user``（按用户分解）—— 只对管理员开放该模式，
            鉴权在 API 层做，这一层只负责查询。

    时间过滤交给数据库比较（用 UTC，与库中写入一致），
    不再手工拼字符串 —— 原实现用 ``isoformat()`` 生成带 'T' 的值去比
    库中 'YYYY-MM-DD HH:MM:SS' 的文本，ASCII 里 'T' > ' '，
    会让**同一天的记录全被误判为晚于起点**。
    """
    since = utcnow() - timedelta(days=days)
    day_col = func.date(TokenUsage.created_at).label("day")
    total_expr = TokenUsage.prompt_tokens + TokenUsage.completion_tokens

    # owner_id 为 None 时不加归属过滤 = 统计全部用户
    filters = [TokenUsage.created_at >= since]
    if owner_id is not None:
        filters.append(TokenUsage.owner_id == owner_id)

    async with session_scope() as session:
        daily = [
            dict(r)
            for r in (
                await session.execute(
                    select(
                        day_col,
                        func.sum(TokenUsage.prompt_tokens).label("prompt_tokens"),
                        func.sum(TokenUsage.completion_tokens).label("completion_tokens"),
                        func.sum(total_expr).label("total_tokens"),
                        func.sum(TokenUsage.reasoning_tokens).label("reasoning_tokens"),
                        func.sum(TokenUsage.cost_estimate).label("cost"),
                    )
                    .where(*filters)
                    .group_by(day_col)
                    .order_by(day_col.asc())
                )
            )
            .mappings()
            .all()
        ]

        model_breakdown = [
            dict(r)
            for r in (
                await session.execute(
                    select(
                        TokenUsage.model.label("model"),
                        func.sum(TokenUsage.prompt_tokens).label("prompt_tokens"),
                        func.sum(TokenUsage.completion_tokens).label("completion_tokens"),
                        func.sum(total_expr).label("total_tokens"),
                        func.sum(TokenUsage.reasoning_tokens).label("reasoning_tokens"),
                        func.sum(TokenUsage.cost_estimate).label("cost"),
                        func.count().label("call_count"),
                    )
                    .where(*filters)
                    .group_by(TokenUsage.model)
                    .order_by(func.sum(TokenUsage.cost_estimate).desc())
                )
            )
            .mappings()
            .all()
        ]

        totals_row = (
            (
                await session.execute(
                    select(
                        func.coalesce(func.sum(TokenUsage.prompt_tokens), 0).label("prompt_tokens"),
                        func.coalesce(func.sum(TokenUsage.completion_tokens), 0).label(
                            "completion_tokens"
                        ),
                        func.coalesce(func.sum(total_expr), 0).label("total_tokens"),
                        func.coalesce(func.sum(TokenUsage.reasoning_tokens), 0).label(
                            "reasoning_tokens"
                        ),
                        func.coalesce(func.sum(TokenUsage.cached_tokens), 0).label("cached_tokens"),
                        func.coalesce(func.sum(TokenUsage.cost_estimate), 0.0).label("total_cost"),
                        func.count().label("total_calls"),
                    ).where(*filters)
                )
            )
            .mappings()
            .one()
        )

        # 按用户分解 —— 只有管理员视角（owner_id=None）才需要。
        # 必须 join User 拿用户名：否则界面上只有一堆数字 owner_id，看不出是谁。
        by_user = []
        if owner_id is None:
            by_user = [
                dict(r)
                for r in (
                    await session.execute(
                        select(
                            TokenUsage.owner_id.label("owner_id"),
                            User.username.label("username"),
                            func.sum(TokenUsage.prompt_tokens).label("prompt_tokens"),
                            func.sum(TokenUsage.completion_tokens).label("completion_tokens"),
                            func.sum(total_expr).label("total_tokens"),
                            func.sum(TokenUsage.cost_estimate).label("cost"),
                            func.count().label("call_count"),
                        )
                        .join(User, User.id == TokenUsage.owner_id, isouter=True)
                        .where(*filters)
                        .group_by(TokenUsage.owner_id)
                        .order_by(func.sum(total_expr).desc())
                    )
                )
                .mappings()
                .all()
            ]

    return {
        "daily": daily,
        "model_breakdown": model_breakdown,
        "totals": dict(totals_row),
        "period_days": days,
        # 未匹配到费率的模型。前端据此提示"成本未知" —— 否则用户会把
        # 一个因为查不到价而永远是 0 的数字，误读成"没花钱"。
        "unmapped_models": sorted(_unmapped_models),
        # 按用户分解（仅管理员视角，owner_id=None 时）。需要 join User 拿用户名，
        # 否则界面上只会是一堆数字 ID。
        "by_user": by_user,
    }
