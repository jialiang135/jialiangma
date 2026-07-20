"""
Token 使用统计与费用估算
跟踪每次 LLM 调用的 token 消耗，支持按用户、按天、按模型聚合统计
"""
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger

# Cost rates per 1M tokens (USD)
COST_RATES = {
    "deepseek-v4-pro": {"input": 0.28, "output": 1.10},
    "qwen-turbo": {"input": 0.50, "output": 0.50},
    "glm-4": {"input": 0.50, "output": 0.50},
    "default": {"input": 0.50, "output": 0.50},
}


def estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数量。
    中文字符约 1 token/1.5 字符，英文字符约 1 token/4 字符。
    """
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '一' <= c <= '鿿')
    other_chars = len(text) - chinese_chars
    estimated = int(chinese_chars * 1.5 + other_chars / 4)
    return max(estimated, 1)


def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """根据模型费率计算单次调用费用（美元）"""
    rates = COST_RATES.get(model, COST_RATES["default"])
    input_cost = (prompt_tokens / 1_000_000) * rates["input"]
    output_cost = (completion_tokens / 1_000_000) * rates["output"]
    return round(input_cost + output_cost, 8)


def track_usage(owner_id: int, model: str, prompt_tokens: int, completion_tokens: int):
    """记录一次 token 使用到 SQLite"""
    cost = calculate_cost(model, prompt_tokens, completion_tokens)
    try:
        from core.database import get_db
        with get_db() as conn:
            conn.execute(
                "INSERT INTO token_usage (owner_id, model, prompt_tokens, completion_tokens, cost_estimate) "
                "VALUES (?, ?, ?, ?, ?)",
                (owner_id, model, prompt_tokens, completion_tokens, cost),
            )
            conn.commit()
        logger.debug(
            f"Token usage tracked: owner={owner_id}, model={model}, "
            f"prompt={prompt_tokens}, completion={completion_tokens}, cost=${cost:.6f}"
        )
    except Exception as e:
        logger.error(f"Failed to track token usage: {e}")


def get_usage_stats(owner_id: int, days: int = 7) -> dict:
    """
    获取用户 token 使用统计。
    返回每日分解、模型分解、总费用等。
    """
    from core.database import get_db

    since = datetime.now() - timedelta(days=days)

    with get_db() as conn:
        # 每日汇总
        rows = conn.execute(
            """SELECT DATE(created_at) as day,
                      SUM(prompt_tokens) as prompt_tokens,
                      SUM(completion_tokens) as completion_tokens,
                      SUM(prompt_tokens + completion_tokens) as total_tokens,
                      SUM(cost_estimate) as cost
               FROM token_usage
               WHERE owner_id = ? AND created_at >= ?
               GROUP BY DATE(created_at)
               ORDER BY day ASC""",
            (owner_id, since.isoformat()),
        ).fetchall()

        daily = [dict(r) for r in rows]

        # 模型分解
        model_rows = conn.execute(
            """SELECT model,
                      SUM(prompt_tokens) as prompt_tokens,
                      SUM(completion_tokens) as completion_tokens,
                      SUM(prompt_tokens + completion_tokens) as total_tokens,
                      SUM(cost_estimate) as cost,
                      COUNT(*) as call_count
               FROM token_usage
               WHERE owner_id = ? AND created_at >= ?
               GROUP BY model
               ORDER BY cost DESC""",
            (owner_id, since.isoformat()),
        ).fetchall()

        model_breakdown = [dict(r) for r in model_rows]

        # 总计
        total_row = conn.execute(
            """SELECT COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                      COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                      COALESCE(SUM(prompt_tokens + completion_tokens), 0) as total_tokens,
                      COALESCE(SUM(cost_estimate), 0) as total_cost,
                      COUNT(*) as total_calls
               FROM token_usage
               WHERE owner_id = ? AND created_at >= ?""",
            (owner_id, since.isoformat()),
        ).fetchone()

        totals = dict(total_row) if total_row else {}

    return {
        "daily": daily,
        "model_breakdown": model_breakdown,
        "totals": totals,
        "period_days": days,
    }
