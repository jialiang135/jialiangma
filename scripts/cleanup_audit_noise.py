"""清理审计表里的历史噪声。

**规则与运行时完全同源**：直接复用 `core.audit.should_audit` —— 运行时用它决定
"这条要不要写"，这里用它决定"这条要不要删"。两边各写一套判断必然漂移
（删掉的类型还在写、或反过来），所以这里绝不重复实现规则。

会删的（"HTTP 形态的机械请求"）：
  - 非 `/api/` 的路径：SPA 首页、静态资源、`/favicon.ico`，以及**互联网扫描器的
    探测**（`/index.php`、`/SDK/webLanguage`、`/robots.txt`……）
  - `/api/` 下的普通读（读自己的数据不是审计事件）
  - 成功登录在中间件留下的那条**重复**记录（路由自己已记了一条语义审计）

会留的：
  - **语义事件**：`login` / `register` / `delete_file` 等（action 不是 "METHOD /path" 形态）
  - 写操作（POST/PUT/PATCH/DELETE）
  - `/api/admin/*`（管理员触及他人数据）
  - **失败**的登录/注册（爆破检测唯一的证据来源）

用法：
    python scripts/cleanup_audit_noise.py            # 干跑，只报告不删
    python scripts/cleanup_audit_noise.py --apply    # 真删（先自动备份数据库）
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audit import is_http_noise
from core.db.engine import backup_database


async def main() -> int:
    apply = "--apply" in sys.argv

    from sqlalchemy import text

    from core.db.engine import get_db_path, session_scope

    db = get_db_path()
    print(f"审计表: {db}")

    async with session_scope() as session:
        rows = (
            await session.execute(text("SELECT id, action, status, created_at FROM audit_log"))
        ).fetchall()

    total = len(rows)
    noise = [r for r in rows if is_http_noise(r[1], r[2])]
    keep = [r for r in rows if not is_http_noise(r[1], r[2])]

    print(f"\n共 {total} 行 → 噪声 {len(noise)} 行（{len(noise) * 100 // max(1, total)}%），保留 {len(keep)} 行")
    print("\n噪声构成（前 10）:")
    for action, n in Counter(r[1] for r in noise).most_common(10):
        print(f"   {action[:52]:54s} {n}")

    print("\n会保留的（前 10）:")
    for action, n in Counter(r[1] for r in keep).most_common(10):
        print(f"   {action[:52]:54s} {n}")

    if not noise:
        print("\n没有噪声，无需清理")
        return 0

    if not apply:
        print("\n[干跑] 要真删请加 --apply（会先自动备份数据库）")
        return 0

    # 删之前先备份：这是不可逆操作，而审计表本来就是"出事后要用"的东西
    backup_path = db.parent / "backups" / f"before_audit_cleanup_{time.strftime('%Y%m%d_%H%M%S')}.db"
    await backup_database(backup_path)
    print(f"\n已备份到: {backup_path}")

    ids = [r[0] for r in noise]
    async with session_scope() as session:
        # 分批删，避免一条 SQL 里塞上万个 id
        for i in range(0, len(ids), 500):
            batch = ids[i : i + 500]
            await session.execute(
                text(f"DELETE FROM audit_log WHERE id IN ({','.join(str(x) for x in batch)})")
            )
    print(f"已删除 {len(ids)} 行噪声")

    async with session_scope() as session:
        left = (await session.execute(text("SELECT COUNT(*) FROM audit_log"))).scalar_one()
    print(f"剩余 {left} 行")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("ALLOW_INSECURE_DEFAULTS", "true")
    raise SystemExit(asyncio.run(main()))
