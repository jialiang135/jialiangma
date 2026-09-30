"""线上部署的端到端 UI 验收（真实无头浏览器）。

为什么需要它：**后端的测试再多也测不到"界面是不是真的在按预期动"**。
前端整站重构时没有任何自动化测试兜底，于是 `send()` 里的
`input.value = ''` 在拆分组件时被漏掉，直到人工浏览器测试才被发现
（报告 BUG-03）。这个脚本把那次人工验收固化成一条可重复的命令。

它验三件事（都是曾经真实出过问题的点）：
  1. 未登录访问 /chat 会被路由守卫弹到 /login
  2. **发送后输入框要清空**，且回答完成后仍然是空的
  3. **推理步骤不能重复**（根 run 的 on_chain_end 带累积 state 那个坑），
     以及 **历史侧栏要自动出现新对话**（不点刷新）

顺带断言控制台零 error。每个断言失败都会打印实际值，便于定位。

用法（凭据从环境变量读，不写进仓库 —— 这是公开仓库）：

    UI_USER=xxx UI_PASSWORD=yyy python scripts/verify_deployed_ui.py
    # 可选：BASE_URL=http://127.0.0.1:7860 验本地

前置：`pip install playwright && playwright install chromium`。

注意：脚本会**真的发一条消息**，因此会消耗一次 LLM 额度并在该账号下留下
一条对话记录（标题固定为下面的 QUESTION，跑完可在侧栏手动删掉）。
"""

from __future__ import annotations

import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE_URL = os.getenv("BASE_URL", "http://193.112.29.164:8080").rstrip("/")
USERNAME = os.getenv("UI_USER", "")
PASSWORD = os.getenv("UI_PASSWORD", "")

# 固定成一句话，方便跑完在侧栏认出来并清理
QUESTION = "介绍一下你自己"


async def main() -> int:
    if not USERNAME or not PASSWORD:
        print("缺少凭据：请用 UI_USER / UI_PASSWORD 环境变量提供测试账号。")
        print(__doc__)
        return 2

    ok = True
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # 桌面宽度：侧栏在窄屏下是抽屉式（移出屏幕外），看不到条目
        ctx = await browser.new_context(viewport={"width": 1440, "height": 950})
        page = await ctx.new_page()

        logs: list[str] = []
        page.on(
            "console", lambda m: logs.append(f"[{m.type}] {m.text}") if m.type == "error" else None
        )
        page.on("pageerror", lambda e: logs.append(f"[pageerror] {e}"))

        # ── 1. 路由守卫 ──
        await page.goto(f"{BASE_URL}/chat", wait_until="networkidle")
        await page.wait_for_timeout(1500)
        if "/login" in page.url:
            print(f"[OK]   未登录访问 /chat → {page.url}")
        else:
            print(f"[FAIL] 未登录访问 /chat 没被弹到登录页，停在 {page.url}")
            ok = False

        await page.fill('input[placeholder="请输入用户名"]', USERNAME)
        await page.fill('input[placeholder="请输入密码"]', PASSWORD)
        await page.click('button[type="submit"]')
        await page.wait_for_url("**/chat", timeout=25000)
        await page.wait_for_selector(".composer-input", timeout=15000)
        print(f"[OK]   登录成功 → {page.url}")

        # 发送前的侧栏条目数 + 线上 HTML 引用的资源能不能取到（白屏排查）
        before = await page.locator(".sidebar-list .conv-title").all_inner_texts()
        print(f"[INFO] 发送前侧栏 {len(before)} 条")

        # ── 2. 手动输入 + 回车：输入框必须清空 ──
        box = page.locator(".composer-input")
        await box.fill(QUESTION)
        await box.press("Enter")
        await page.wait_for_timeout(600)

        right_after = await box.input_value()
        if right_after.strip():
            print(f"[FAIL] 回车后输入框没清空: {right_after!r}")
            ok = False
        else:
            print("[OK]   回车后输入框已清空")

        # 等回答结束：生成中是「停止」按钮，结束后变回「发送」
        try:
            await page.wait_for_selector(".composer-btn.is-stop", timeout=20000)
        except Exception:
            print("[INFO] 没观察到停止按钮（可能瞬时答完）")
        await page.wait_for_selector(".composer-btn.is-send", timeout=240000)
        await page.wait_for_timeout(800)

        final_value = await box.input_value()
        if final_value.strip():
            print(f"[FAIL] 回答完成后输入框里还留着文字: {final_value!r}")
            ok = False
        else:
            print("[OK]   回答完成后输入框仍为空")

        # ── 3. 推理步骤不重复 ──
        steps = await page.locator(".reasoning .step-text").all_inner_texts()
        dups = sorted({s for s in steps if steps.count(s) > 1})
        if not steps:
            print("[FAIL] 一条推理步骤都没有 —— 推理面板没渲染出来")
            ok = False
        elif dups:
            print(f"[FAIL] 推理步骤重复: {dups}")
            ok = False
        else:
            print(f"[OK]   推理步骤 {len(steps)} 条，无重复")

        # ── 4. 侧栏自动刷新（不点刷新按钮）──
        after = await page.locator(".sidebar-list .conv-title").all_inner_texts()
        if len(after) > len(before):
            print(f"[OK]   侧栏自动多出新对话（{len(before)} → {len(after)}）")
        else:
            print(f"[FAIL] 侧栏没自动刷新（{len(before)} → {len(after)}）")
            ok = False

        # ── 5. 控制台 ──
        if logs:
            print(f"[FAIL] 控制台有 {len(logs)} 条错误:")
            for line in logs[:5]:
                print("       ", line[:180])
            ok = False
        else:
            print("[OK]   控制台零错误")

        await browser.close()

    print("\n判定:", "全部通过" if ok else "有未通过项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
