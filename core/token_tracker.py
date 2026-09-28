"""
Token 使用统计与费用估算
跟踪每次 LLM 调用的 token 消耗，支持按用户、按天、按模型聚合统计
"""
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger

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

# SQLite 的 created_at 由 CURRENT_TIMESTAMP 写入，格式为 'YYYY-MM-DD HH:MM:SS'。
# 用它做字符串比较时绑定值必须是同一格式：isoformat() 产生的
# 'YYYY-MM-DDTHH:MM:SS' 里 'T'(0x54) > ' '(0x20)，会让**同一天的记录
# 全部被误判为"晚于起点"**。
_SQLITE_DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


def estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数量。
    中文字符约 1 token/1.5 字符，英文字符约 1 token/4 字符。

    仅在拿不到真实 usage 时作为兜底；正常路径应使用 provider 返回的
    ``usage_metadata``（见 core/llm.py 的 extract_usage）。
    """
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '一' <= c <= '鿿')
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


def track_usage(
    owner_id: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int = 0,
    cached_tokens: int = 0,
):
    """
    记录一次 token 使用到 SQLite。

    Args:
        prompt_tokens:     输入 token（含命中 prompt 缓存的部分）。
        completion_tokens: 输出 token。**已包含** reasoning_tokens ——
                           这是 provider 的计费口径。
        reasoning_tokens:  其中用于思考的 token 数，单独留档便于分析。
        cached_tokens:     命中 prompt 缓存的输入 token 数。
    """
    cost = calculate_cost(model, prompt_tokens, completion_tokens)
    try:
        from core.database import get_db
        with get_db() as conn:
            conn.execute(
                "INSERT INTO token_usage "
                "(owner_id, model, prompt_tokens, completion_tokens, "
                " reasoning_tokens, cached_tokens, cost_estimate) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (owner_id, model, prompt_tokens, completion_tokens,
                 reasoning_tokens, cached_tokens, cost),
            )
            conn.commit()
        logger.debug(
            "Token usage tracked: owner={}, model={}, prompt={}, completion={} "
            "(reasoning={}, cached={}), cost=${:.6f}",
            owner_id, model, prompt_tokens, completion_tokens,
            reasoning_tokens, cached_tokens, cost,
        )
    except Exception as e:
        logger.error(f"Failed to track token usage: {e}")


def get_usage_stats(owner_id: int, days: int = 7) -> dict:
    """
    获取用户 token 使用统计。
    返回每日分解、模型分解、总费用等。
    """
    from core.database import get_db

    # 用与 SQLite CURRENT_TIMESTAMP 一致的格式，避免 isoformat 的 'T' 比较问题
    since = (datetime.now() - timedelta(days=days)).strftime(_SQLITE_DATETIME_FMT)

    with get_db() as conn:
        # 每日汇总
        rows = conn.execute(
            """SELECT DATE(created_at) as day,
                      SUM(prompt_tokens) as prompt_tokens,
                      SUM(completion_tokens) as completion_tokens,
                      SUM(prompt_tokens + completion_tokens) as total_tokens,
                      SUM(reasoning_tokens) as reasoning_tokens,
                      SUM(cost_estimate) as cost
               FROM token_usage
               WHERE owner_id = ? AND created_at >= ?
               GROUP BY DATE(created_at)
               ORDER BY day ASC""",
            (owner_id, since),
        ).fetchall()

        daily = [dict(r) for r in rows]

        # 模型分解
        model_rows = conn.execute(
            """SELECT model,
                      SUM(prompt_tokens) as prompt_tokens,
                      SUM(completion_tokens) as completion_tokens,
                      SUM(prompt_tokens + completion_tokens) as total_tokens,
                      SUM(reasoning_tokens) as reasoning_tokens,
                      SUM(cost_estimate) as cost,
                      COUNT(*) as call_count
               FROM token_usage
               WHERE owner_id = ? AND created_at >= ?
               GROUP BY model
               ORDER BY cost DESC""",
            (owner_id, since),
        ).fetchall()

        model_breakdown = [dict(r) for r in model_rows]

        # 总计
        total_row = conn.execute(
            """SELECT COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                      COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                      COALESCE(SUM(prompt_tokens + completion_tokens), 0) as total_tokens,
                      COALESCE(SUM(reasoning_tokens), 0) as reasoning_tokens,
                      COALESCE(SUM(cached_tokens), 0) as cached_tokens,
                      COALESCE(SUM(cost_estimate), 0) as total_cost,
                      COUNT(*) as total_calls
               FROM token_usage
               WHERE owner_id = ? AND created_at >= ?""",
            (owner_id, since),
        ).fetchone()

        totals = dict(total_row) if total_row else {}

    return {
        "daily": daily,
        "model_breakdown": model_breakdown,
        "totals": totals,
        "period_days": days,
    }
