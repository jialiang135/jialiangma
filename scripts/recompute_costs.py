"""
按当前费率重算历史 token 成本
=============================

**什么时候用**：改了 ``LLM_COST_RATES``（或换了模型/价格）之后。

为什么需要它：``token_usage.cost_estimate`` 是**调用时算好写库的快照**。
费率表里查不到模型时，那次调用就永久记成 0 —— 后来补上费率，
**历史记录不会自动跟着变**，界面上就会显示"39 次调用、费用 ¥0"。

这也是本项目的真实情况：``deepseek-flash`` 长期不在费率表里，
所以 2026-09-29 之前的所有记录成本都是 0。

用法
----
::

    cd personal_agent
    python scripts/recompute_costs.py --dry-run   # 先看会改多少条
    python scripts/recompute_costs.py             # 实际写库

只改 ``cost_estimate`` 一个字段，**不动 token 数**——token 数是从 provider
的 usage 记下来的真实值，重算成本不该碰它。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

from sqlalchemy import select  # noqa: E402

from core.db.engine import session_scope
from core.db.models import TokenUsage
from core.token_tracker import COST_RATES, calculate_cost  # noqa: E402


async def main(dry_run: bool) -> int:
    async with session_scope() as session:
        rows = (await session.execute(select(TokenUsage))).scalars().all()
        print(f"共 {len(rows)} 条记录")

        unknown: set[str] = set()
        changed = 0
        delta = 0.0

        for r in rows:
            if r.model not in COST_RATES:
                unknown.add(r.model)
                continue
            new_cost = calculate_cost(
                r.model, r.prompt_tokens, r.completion_tokens, cached_tokens=r.cached_tokens
            )
            old_cost = float(r.cost_estimate or 0.0)
            if abs(new_cost - old_cost) < 1e-9:
                continue
            changed += 1
            delta += new_cost - old_cost
            if not dry_run:
                r.cost_estimate = new_cost

        if not dry_run:
            await session.commit()

    print(f"需要改: {changed} 条")
    print(f"成本变化合计: ¥{delta:+.4f}")
    if unknown:
        print(f"⚠️ 费率表里仍然没有的模型（这些条目跳过、成本保持原值）: {sorted(unknown)}")
        print("   补费率：在 config/.env 里设 LLM_COST_RATES")
    print("\n这是 dry-run，没有写库。" if dry_run else "\n已写库。")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="按当前费率重算历史 token 成本")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run)))
