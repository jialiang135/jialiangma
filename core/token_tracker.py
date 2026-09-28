"""
Token 使用统计与费用估算
跟踪每次 LLM 调用的 token 消耗，支持按用户、按天、按模型聚合统计
"""

from datetime import timedelta

from loguru import logger
from sqlalchemy import func, select

from core.database import TokenUsage, session_scope, utcnow

# Cost rates per 1M tokens (USD)。
# 只填**确知**的费率：表里没有的模型不会套用"默认费率"编一个数字，
# 而是记为 0 并打告警 —— 编造成本比没有成本更糟。
COST_RATES = {
    "deepseek-v4-pro": {"input": 0.28, "output": 1.10},
    "deepseek-chat": {"input": 0.28, "output": 1.10},
    "qwen-turbo": {"input": 0.50, "output": 0.50},
    "glm-4": {"input": 0.50, "output": 0.50},
}

# 每个未知模型只告警一次，避免刷日志
_warned_unknown_models: set[str] = set()


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


def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """
    根据模型费率计算单次调用费用（美元）。

    费率表中没有该模型时返回 0 并首次告警，不套用默认费率 ——
    宁可显示"成本未知"，也不要显示一个编造的数字。
    """
    rates = COST_RATES.get(model)
    if rates is None:
        if model not in _warned_unknown_models:
            _warned_unknown_models.add(model)
            logger.warning(
                "模型 {} 不在 COST_RATES 费率表中，成本记为 0。"
                "如需成本统计，请在 core/token_tracker.py 补充其真实费率",
                model,
            )
        return 0.0

    input_cost = (prompt_tokens / 1_000_000) * rates["input"]
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
    cost = calculate_cost(model, prompt_tokens, completion_tokens)
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


async def get_usage_stats(owner_id: int, days: int = 7) -> dict:
    """
    获取用户 token 使用统计：每日分解、模型分解、总计。

    时间过滤交给数据库比较（用 UTC，与库中写入一致），
    不再手工拼字符串 —— 原实现用 ``isoformat()`` 生成带 'T' 的值去比
    库中 'YYYY-MM-DD HH:MM:SS' 的文本，ASCII 里 'T' > ' '，
    会让**同一天的记录全被误判为晚于起点**。
    """
    since = utcnow() - timedelta(days=days)
    day_col = func.date(TokenUsage.created_at).label("day")
    total_expr = TokenUsage.prompt_tokens + TokenUsage.completion_tokens

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
                    .where(TokenUsage.owner_id == owner_id, TokenUsage.created_at >= since)
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
                    .where(TokenUsage.owner_id == owner_id, TokenUsage.created_at >= since)
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
                    ).where(TokenUsage.owner_id == owner_id, TokenUsage.created_at >= since)
                )
            )
            .mappings()
            .one()
        )

    return {
        "daily": daily,
        "model_breakdown": model_breakdown,
        "totals": dict(totals_row),
        "period_days": days,
    }
