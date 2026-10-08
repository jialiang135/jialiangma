"""全页面交互巡检：每个页面/标签都走一遍，断言"界面显示的是真数据"而不是空。

为什么需要它：之前踩过的坑都是**静默空白** ——
  - 用量页读了 `res.totals`，而接口返回 `{success, message, data:{totals}}` → 永远显示空
  - 切块卡片的三个输入框因漏 import 而不渲染 → 只剩标签
这类问题**构建通过、控制台不报错、后端接口也正常**，只有"界面到底显示了什么"
能发现。所以本脚本不看"有没有报错"，只看**该出现的内容有没有出现**。

用法：
    UI_USER=xxx UI_PASSWORD=yyy python scripts/audit_interactions.py [BASE_URL]
"""

from __future__ import annotations

import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("BASE_URL") or "").rstrip("/")
USERNAME = os.getenv("UI_USER", "")
PASSWORD = os.getenv("UI_PASSWORD", "")

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))


async def _texts(page, selector: str) -> list[str]:
    try:
        return [t.strip() for t in await page.locator(selector).all_inner_texts() if t.strip()]
    except Exception:
        return []


async def _has_any(page, *selectors: str) -> bool:
    for s in selectors:
        try:
            if await page.locator(s).count() > 0:
                return True
        except Exception:
            continue
    return False


async def audit_chat(page) -> None:
    """对话页：发一条消息，断言回答、证据、推理步骤都出现了。"""
    await page.goto(f"{BASE_URL}/chat", wait_until="networkidle")
    await page.wait_for_timeout(1500)
    box = page.locator(".composer-input")
    await box.fill("介绍一下你自己")
    await box.press("Enter")
    await page.wait_for_selector(".composer-btn.is-stop", timeout=20000)
    await page.wait_for_selector(".composer-btn.is-send", timeout=240000)
    await page.wait_for_timeout(800)

    body = await page.locator(".chat-stream").inner_text()
    check(len(body.strip()) > 60, "对话页：回答有实际内容", f"{len(body)} 字")
    check(
        await page.locator(".reasoning .step-text").count() > 0,
        "对话页：推理步骤渲染出来了",
    )
    ev = await page.locator(".evidence, [class*=evidence]").count()
    check(ev > 0, "对话页：证据轨出现了", f"count={ev}")


async def audit_knowledge(page) -> None:
    """知识库页：文件表、切块卡片、试切预览、查看抽屉。"""
    await page.goto(f"{BASE_URL}/knowledge", wait_until="networkidle")
    await page.wait_for_timeout(2000)
    if await page.locator(".ui-modal-close").count():
        await page.locator(".ui-modal-close").first.click()
        await page.wait_for_timeout(400)

    rows = await page.locator("table tbody tr, .ui-table tbody tr").count()
    check(rows > 0, "知识库：文件表有数据行", f"{rows} 行")

    controls = await page.evaluate(
        """() => Array.from(document.querySelectorAll('.chunking-field')).map(el => {
            const c = el.querySelector('input, select');
            return {label: el.querySelector('.chunking-label')?.textContent.trim(),
                    visible: c ? c.getBoundingClientRect().width > 10 : false};
        })"""
    )
    check(
        len(controls) >= 4 and all(c["visible"] for c in controls),
        "知识库：切块设置 4 个控件都渲染",
        str([(c["label"], c["visible"]) for c in controls]),
    )

    await page.locator(".chunking-preview button").first.click()
    try:
        await page.wait_for_selector(".preview-compare", timeout=90000)
        txt = await page.locator(".preview-compare").inner_text()
        check("块" in txt, "知识库：试切预览给出了块数对比", txt.replace("\n", " ")[:80])
    except Exception as e:
        check(False, "知识库：试切预览没有出结果", str(e)[:100])

    view = page.locator("button", has_text="查看")
    if await view.count():
        await view.first.click()
        await page.wait_for_selector(".ui-drawer", timeout=10000)
        await page.locator(".ui-tab", has_text="入库切片").click()
        try:
            await page.wait_for_selector(".chunk", timeout=20000)
            n = await page.locator(".chunk").count()
            check(n > 0, "知识库：切片视图有内容", f"{n} 块")
        except Exception as e:
            check(False, "知识库：切片视图没渲染", str(e)[:100])
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)


async def audit_eval(page) -> None:
    """评测页：评测集下拉、指标按钮、历史报告表。"""
    await page.goto(f"{BASE_URL}/eval", wait_until="networkidle")
    await page.wait_for_timeout(2000)
    opts = await page.evaluate(
        "() => Array.from(document.querySelectorAll('select option')).map(o => o.textContent.trim())"
    )
    check(len(opts) > 1, "评测页：评测集下拉有选项", str(opts[:4]))
    check(await _has_any(page, "table tbody tr"), "评测页：历史报告表有行")

    # 详情：有报告时可展开
    detail_btn = page.locator("button", has_text="详情")
    if await detail_btn.count():
        await detail_btn.first.click()
        await page.wait_for_timeout(1500)
        cards = await page.locator(".score-card").count()
        facts = await page.locator(".fact").count()
        check(cards > 0, "评测页：详情有指标卡", f"{cards} 张")
        check(facts > 0, "评测页：详情有样本构成", f"{facts} 项")


async def audit_stats(page) -> None:
    """用量页：总量卡片、按模型、按日都要有数。"""
    await page.goto(f"{BASE_URL}/stats", wait_until="networkidle")
    await page.wait_for_timeout(2000)
    # 断言"总量卡片显示的是真数字"，而不是"页面渲染完成"这种空转判据
    # （第一版这里写了 `... or True`，等于没测 —— 空转的断言比没有更糟）
    cards = await page.locator("[class*=stat], [class*=card], .kpi").all_inner_texts()
    joined = " ".join(cards)
    check(any(ch.isdigit() for ch in joined), "用量页：指标卡片有数字", joined[:90])
    check("总 Token" in joined or "Token" in joined, "用量页：有 Token 总量卡片", joined[:60])
    # 管理员切到「全部用户」应仍有数据（这是权限分支，容易只测到一半）
    scope_tabs = page.locator(".ui-tab", has_text="全部用户")
    if await scope_tabs.count():
        await scope_tabs.first.click()
        await page.wait_for_timeout(2000)
        all_txt = " ".join(await page.locator("[class*=stat], [class*=card], .kpi").all_inner_texts())
        check(any(ch.isdigit() for ch in all_txt), "用量页：切「全部用户」后仍有数字", all_txt[:70])


async def audit_admin(page) -> None:
    """管理后台 6 个 Tab 逐个点：每个都要么有数据、要么有明确的空态/错误，不能静默空白。"""
    await page.goto(f"{BASE_URL}/admin", wait_until="networkidle")
    await page.wait_for_timeout(2000)

    tabs = await page.locator(".ui-tab").all_inner_texts()
    check(len(tabs) >= 5, "管理页：Tab 数量正常", str([t.strip() for t in tabs]))

    for i, label in enumerate([t.strip() for t in tabs]):
        try:
            await page.locator(".ui-tab").nth(i).click()
            await page.wait_for_timeout(2500)
            area = await page.locator("main, .admin, body").first.inner_text()
            # 判据要覆盖三种正常形态：**表格行 / 统计卡片 / 明确的空态或告警**。
            # 第一版只认表格与空态，于是"用卡片布局的仪表盘"被误判成没数据。
            has_rows = await _has_any(page, "table tbody tr")
            has_cards = await _has_any(page, ".kpi", "[class*=card]")
            has_notice = await _has_any(page, ".ui-empty", ".ui-alert")
            has_number = any(ch.isdigit() for ch in area)
            ok = len(area.strip()) > 40 and (has_rows or has_cards or has_notice) and (
                has_number or has_notice
            )
            check(
                ok,
                f"管理页 Tab「{label}」：有数据或明确空态",
                f"行={has_rows} 卡片={has_cards} 空态/告警={has_notice} 数字={has_number}",
            )
        except Exception as e:
            check(False, f"管理页 Tab「{label}」：点击/渲染异常", str(e)[:90])


async def main() -> int:
    if not BASE_URL or not USERNAME or not PASSWORD:
        print("用法: UI_USER=x UI_PASSWORD=y python scripts/audit_interactions.py [BASE_URL]")
        return 2

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 1000})
        page = await ctx.new_page()
        warns: list[str] = []
        page.on(
            "console",
            lambda m: warns.append(f"[{m.type}] {m.text}")
            if m.type in ("error", "warning")
            else None,
        )
        page.on("pageerror", lambda e: warns.append(f"[pageerror] {e}"))

        await page.goto(f"{BASE_URL}/login", wait_until="networkidle")
        await page.fill('input[placeholder="请输入用户名"]', USERNAME)
        await page.fill('input[placeholder="请输入密码"]', PASSWORD)
        await page.click('button[type="submit"]')
        await page.wait_for_url("**/chat", timeout=25000)
        await page.evaluate("localStorage.setItem('guide_seen','1')")

        await audit_chat(page)
        await audit_knowledge(page)
        await audit_eval(page)
        await audit_stats(page)
        await audit_admin(page)

        noise = [w for w in warns if "resolve component" in w.lower() or "pageerror" in w]
        check(not noise, "全程：无未解析组件/运行时错误", str(noise[:2]))

        await page.screenshot(path="audit_interactions.png", full_page=False)
        await browser.close()

    print("\n=== 交互巡检 ===")
    failed = 0
    for ok, name, detail in results:
        if not ok:
            failed += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   -> {detail[:110]}" if detail else ""))
    print(f"\n{len(results) - failed}/{len(results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
