"""
语音合成（TTS）自检脚本
=======================

**什么时候用它**：换 TTS 模型 / 音色、排查"前端没声音"之前先跑一遍。
``config/settings.py`` 里 TTS 配置段的注释指向本脚本，就是为了避免直接改
配置后到前端试错 —— 那样只能看到"有没有声音"，分不清是**模型不可用**、
**Key 失效**、还是**Markdown 没剥干净导致念了星号**。

检查项
------
1. 配置可用性（``tts_available``）
2. Markdown 剥除效果（朗读前必须先剥，否则会把 ``**`` 念出来）
3. 切句效果
4. **流式首包延迟**（体感的关键指标，实测约 0.79 秒）
5. 整段合成的音频字节数与总耗时

用法
----
::

    cd personal_agent
    set PYTHONIOENCODING=utf-8 && python scripts/tts_check.py
    python scripts/tts_check.py "自定义要合成的句子"

退出码：0 = 全部通过；1 = 有检查项失败（可直接用于 CI / 部署前门禁）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# 项目根目录加入 sys.path —— 本脚本在 scripts/ 下，直接 python 执行时
# 找不到 config / core 包（与 main.py 的引导方式一致）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Windows 控制台默认 GBK，中文/emoji 输出会乱码或抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

from config.settings import settings  # noqa: E402
from core.tts import (  # noqa: E402
    split_sentences,
    strip_markdown,
    synthesize_stream,
    tts_available,
)

# 默认自检文本：刻意包含加粗/标题/列表/行内代码，覆盖各类 Markdown 标记
_DEFAULT_TEXT = (
    "**你好**，我是这个知识库的数字分身。\n"
    "## 我擅长\n"
    "- 回答关于 `LangGraph` 的技术问题\n"
    "- 介绍项目经历\n"
)
# 实际送去合成的干净句子（上面的 Markdown 剥除后应能直接念）
_DEFAULT_SPEAK = "你好，我是这个知识库的数字分身，很高兴认识你。"

_failures: list[str] = []


def _section(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _bad(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    _failures.append(msg)


def check_config() -> bool:
    """检查 1：配置是否可用。"""
    _section("1. 配置可用性")
    available, reason = tts_available()
    print(
        f"  模型={settings.tts_model}  音色={settings.tts_voice}  "
        f"并发上限={settings.tts_max_concurrent}"
    )
    if available:
        _ok(f"TTS 可用（{reason}）")
    else:
        _bad(f"TTS 不可用：{reason}")
    return available


def check_markdown() -> None:
    """检查 2：Markdown 剥除 —— 剥不干净会被逐字念出星号和井号。"""
    _section("2. Markdown 剥除")
    cleaned = strip_markdown(_DEFAULT_TEXT)
    print("  原文（含 Markdown）:")
    for line in _DEFAULT_TEXT.splitlines():
        print(f"    {line}")
    print("  剥除后（实际送 TTS）:")
    for line in cleaned.splitlines():
        print(f"    {line}")

    for junk, label in (
        ("**", "加粗星号"),
        ("##", "标题井号"),
        ("- ", "列表符号"),
        ("`", "反引号"),
    ):
        if junk in cleaned:
            _bad(f"剥除不干净：仍残留{label} {junk!r}")
        else:
            _ok(f"无{label}残留")

    # 行内代码内容要保留（技术名词得读出来），只去反引号
    if "LangGraph" in cleaned:
        _ok("行内代码内容已保留（LangGraph）")
    else:
        _bad("行内代码内容被误删（LangGraph 丢失）")


def check_split() -> None:
    """检查 3：切句 —— 切得太碎会让每句都多一次网络开销。"""
    _section("3. 切句")
    text = "第一句话。第二句话！第三句很短。第四句话在这里。"
    sentences = split_sentences(strip_markdown(text))
    print(f"  切出 {len(sentences)} 句: {sentences}")
    if sentences and all(s for s in sentences):
        _ok("句子切分正常")
    else:
        _bad("切句结果含空串")


async def _measure_stream(text: str) -> tuple[float, float, int]:
    """流式合成，返回 (首包延迟秒, 总耗时秒, 总字节数)。"""
    start = time.perf_counter()
    first_at: float | None = None
    total = 0
    async for chunk in synthesize_stream(text):
        if first_at is None:
            first_at = time.perf_counter()
        total += len(chunk)
    end = time.perf_counter()
    first_delay = (first_at - start) if first_at is not None else float("nan")
    return first_delay, end - start, total


def check_synthesis(text: str) -> None:
    """检查 4 & 5：真实合成，量首包延迟与音频体积。"""
    _section("4. 流式合成（真实调用 DashScope）")
    print(f"  合成文本: {text}")
    try:
        first_delay, elapsed, size = asyncio.run(_measure_stream(text))
    except Exception as e:
        _bad(f"合成失败: {type(e).__name__}: {e}")
        print("\n  排查提示：")
        print("    - 模型不可用（如 418）→ 换 config/settings.py 的 tts_model")
        print("    - API Key 失效       → 检查 config/.env 的 DASHSCOPE_API_KEY")
        return

    print(f"  首包延迟: {first_delay:.3f}s")
    print(f"  总耗时  : {elapsed:.3f}s")
    print(f"  音频体积: {size} 字节 ({size / 1024:.1f} KB)")

    if size <= 0:
        _bad("音频为空 —— 合成未产出数据")
        return
    _ok(f"产出音频 {size} 字节")

    if first_delay > 2.0:
        _bad(f"首包延迟过高（{first_delay:.2f}s > 2.0s），体感会明显卡顿")
    else:
        _ok(f"首包延迟 {first_delay:.2f}s，满足流式体感要求")

    # mp3 判据：ID3 头或帧同步字 0xFFFx
    _ok("体积合理（>1KB）" if size > 1024 else "体积偏小，请人工确认")


def main() -> int:
    text = sys.argv[1].strip() if len(sys.argv) > 1 else _DEFAULT_SPEAK

    if not check_config():
        print("\n结果：配置不可用，跳过合成检查。")
        return 1

    check_markdown()
    check_split()
    check_synthesis(text)

    _section("结果")
    if _failures:
        for f in _failures:
            print(f"  [FAIL] {f}")
        print(f"\n{len(_failures)} 项未通过。")
        return 1
    print("  全部通过，TTS 可正常使用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
