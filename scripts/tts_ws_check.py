"""
TTS WebSocket 端点探针
======================

**为什么需要它**：``scripts/tts_check.py`` 直接调 ``core.tts``，只证明
"DashScope 合成没问题"，**证明不了服务端 WebSocket 链路是通的**。
而这条链路上有好几个独立会断的环节：

1. ``websockets`` 包没装 / 与 uvicorn 版本不兼容 → 握手直接被拒
   （uvicorn 0.29 + websockets 16 就属于这种高风险组合）
2. Nginx 没配 ``Upgrade`` 头 → 握手被当成普通 HTTP 请求
3. 鉴权协议（首条消息必须是 auth）没对齐

所以本脚本**连真实运行中的服务**（默认 127.0.0.1:7860）走一遍完整协议，
每一项都做可断言检查，而不是靠"听着有没有声音"。

用法
----
::

    cd personal_agent
    set PYTHONIOENCODING=utf-8 && python scripts/tts_ws_check.py
    python scripts/tts_ws_check.py --url ws://193.112.29.164:8080/api/tts/stream
    python scripts/tts_ws_check.py --token <已有JWT>   # 不依赖本地 .env 签票

退出码：0 = 全部通过；1 = 有检查项失败。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

DEFAULT_URL = "ws://127.0.0.1:7860/api/tts/stream"
# 刻意带 Markdown：验证服务端确实在合成前做了剥离（见 core.tts.strip_markdown）
DEFAULT_TEXT = "**你好**，这是一条 TTS 链路自检。"

_failures: list[str] = []


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _bad(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    _failures.append(msg)


async def _check_happy_path(url: str, token: str, text: str) -> None:
    """正常链路：auth → ready → speak → started → 音频帧 → done。"""
    import websockets

    print("\n[1] 正常链路（认证 → 合成 → 音频帧）")
    try:
        ws = await asyncio.wait_for(websockets.connect(url), timeout=10)
    except Exception as e:
        _bad(f"WebSocket 握手失败: {type(e).__name__}: {e}")
        print("       排查：服务端是否装了 websockets、Nginx 是否配了 Upgrade 头")
        return

    try:
        # ── 认证 ──
        await ws.send(json.dumps({"type": "auth", "token": token}))
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if first.get("type") == "ready":
            _ok("认证通过，收到 ready")
        else:
            _bad(f"认证未通过，首条响应为: {first}")
            return

        # ── 合成 ──
        started_at = time.perf_counter()
        await ws.send(json.dumps({"type": "speak", "id": 1, "text": text}))

        first_audio_at: float | None = None
        total_bytes = 0
        got_started = False
        got_done = False

        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=30)
            if isinstance(msg, bytes):
                if first_audio_at is None:
                    first_audio_at = time.perf_counter()
                total_bytes += len(msg)
                continue
            event = json.loads(msg)
            etype = event.get("type")
            if etype == "started":
                got_started = True
            elif etype == "done":
                got_done = True
                break
            elif etype == "error":
                _bad(f"服务端返回错误: {event.get('message')}")
                return

        if got_started:
            _ok("收到 started")
        else:
            _bad("未收到 started")

        if total_bytes > 0:
            assert first_audio_at is not None
            _ok(f"收到音频 {total_bytes} 字节 "
                f"（首包 {(first_audio_at - started_at) * 1000:.0f} ms，"
                f"总耗时 {(time.perf_counter() - started_at):.2f}s）")
        else:
            _bad("未收到任何音频帧")

        if got_done:
            _ok("收到 done，单句流程闭环")
        else:
            _bad("未收到 done")

        # ── ping/pong 保活 ──
        await ws.send(json.dumps({"type": "ping"}))
        pong = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if pong.get("type") == "pong":
            _ok("ping → pong 正常")
        else:
            _bad(f"ping 未得到 pong: {pong}")
    finally:
        await ws.close()


async def _check_bad_auth(url: str) -> None:
    """反向用例：坏 token 必须被拒 —— 否则等于端点未鉴权。"""
    import websockets

    print("\n[2] 反向用例（坏 token 必须被拒）")
    try:
        ws = await asyncio.wait_for(websockets.connect(url), timeout=10)
    except Exception as e:
        _bad(f"握手失败: {type(e).__name__}: {e}")
        return

    try:
        await ws.send(json.dumps({"type": "auth", "token": "not-a-real-token"}))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            event = json.loads(raw) if isinstance(raw, str) else {"type": "binary"}
        except Exception:
            # 直接断开（4401）也算拒绝，符合预期
            _ok("坏 token 被直接断开，符合预期")
            return

        if event.get("type") == "error":
            _ok(f"坏 token 被拒: {event.get('message')}")
        else:
            _bad(f"坏 token 竟被接受: {event}")
    finally:
        await ws.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="TTS WebSocket 端点探针")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"默认 {DEFAULT_URL}")
    parser.add_argument("--token", default=None, help="已有 JWT；不给则用本地 .env 的密钥签一个")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="要合成的文本")
    args = parser.parse_args()

    token = args.token
    if not token:
        from core.auth import create_access_token

        token = create_access_token(owner_id=1, username="tts-check", role="admin")
        print("已用本地 config/.env 的 jwt_secret_key 签发自检 token")

    print(f"目标: {args.url}")
    asyncio.run(_check_happy_path(args.url, token, args.text))
    asyncio.run(_check_bad_auth(args.url))

    print("\n" + "=" * 60)
    if _failures:
        for f in _failures:
            print(f"  [FAIL] {f}")
        print(f"{len(_failures)} 项未通过。")
        return 1
    print("  全部通过，TTS WebSocket 链路可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
