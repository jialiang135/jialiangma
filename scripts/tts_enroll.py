"""
声音复刻工具（Voice Enrollment）
===============================

把一段录音变成可用的音色 ID，之后 ``config/settings.py`` 的 ``tts_voice`` 指向它，
TTS 就用你的声音说话。

关键约束（**已实测**，不是照文档猜的）
--------------------------------------
``create_voice(target_model, prefix, url, ...)`` 收的是**公网 http(s) 直链**，
既不是本地文件路径，也不是 ``oss://``。三条路都试过：

1. 本地文件路径 → 不行，接口只认 URL
2. SDK 自带 ``OssUtils.upload`` → 上传成功，但返回 ``oss://dashscope-instant/...``；
   拿去 create_voice 报
   ``InvalidParameter: audio url should start with http or https``
3. 把上传 ACL 改成 ``public-read``，想拼出 https 直链 →
   OSS 直接拒绝：
   ``Policy Condition failed: ["eq","$x-oss-object-acl","private"]``
   —— 策略写死了必须 private，**所以 DashScope 的临时上传永远给不出公网直链**

**结论：录音必须由你自己放在一个公网可访问的 http(s) 地址上。**
DashScope 的服务器在北京，所以要选国内也能访问的地址。

``prefix`` 只能是**数字和小写字母、长度小于 10**（SDK 写死的要求）。

子命令
------
::

    python scripts/tts_enroll.py check                    # 服务是否可用 + 已有音色
    python scripts/tts_enroll.py list
    python scripts/tts_enroll.py enroll --url https://... --name myvoice --test-only
                                                          # 建完验证再删，用来测地址通不通
    python scripts/tts_enroll.py enroll --url https://... --name myvoice
                                                          # 正式创建，保留音色
    python scripts/tts_enroll.py use <voice_id>           # 写入 TTS_MODEL + TTS_VOICE
    python scripts/tts_enroll.py delete <voice_id>

**模型和音色必须同代配套**：音色 ID 里写死了基底模型，混用会返回 418。
例：``cosyvoice-v3.5-flash-bailian-xxx`` 只能用 ``cosyvoice-v3.5-flash`` 合成；
而 v3.5 模型**不认 v2 系的预置音色**（``longxiaochun_v2`` 在 v3.5 下同样报 418），
所以 ``use`` 会把模型一起改掉。

注意：声音复刻是**计费**服务，创建音色会产生费用（``--test-only`` 也会，只是建完就删）。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

from config.settings import settings  # noqa: E402

_PREFIX_RE = re.compile(r"^[0-9a-z]{1,9}$")

# 已知的 CosyVoice 基底模型，长的排前面（v3.5 要在 v3 之前匹配）。
# 音色 ID 形如 ``cosyvoice-v3.5-flash-bailian-<hash>``，前缀就是它的基底模型；
# 合成时必须用同一个模型，否则引擎返回 418 —— 实测过，不是猜测。
_KNOWN_MODELS = (
    "cosyvoice-v3.5-flash",
    "cosyvoice-v3-flash",
    "cosyvoice-v2",
    "cosyvoice-v1",
)


def _infer_model(voice_id: str) -> str | None:
    """从音色 ID 前缀推出它必须配套的合成模型。"""
    for model in _KNOWN_MODELS:
        if voice_id.startswith(model):
            return model
    return None


# 自检用的合成文本：覆盖四声与常见韵母，长度约 20 秒
_PROBE_TEXT = (
    "你好，我是这个知识库的数字分身。"
    "我擅长回答技术问题，也能介绍项目经历。"
    "先从最基础的部分开始，然后逐层深入，"
    "如果你有具体想了解的方向，直接问我就好。"
)


def _service():
    from dashscope.audio.tts_v2 import VoiceEnrollmentService

    return VoiceEnrollmentService(api_key=settings.dashscope_api_key)


def _check_prefix(prefix: str) -> None:
    if not _PREFIX_RE.match(prefix):
        raise SystemExit(f"前缀 {prefix!r} 不合法：只能用小写字母和数字，且长度小于 10")


def _check_url(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise SystemExit(
            f"地址 {url!r} 不合法：必须是 http:// 或 https:// 开头。\n"
            "（本地文件路径和 oss:// 都不行 —— 原因见本文件顶部说明）"
        )


def _synth_with_voice(text: str, voice: str) -> bytes:
    """
    用指定音色合成一段音频。

    直接改 ``settings.tts_voice`` —— ``core.tts._build_synthesizer`` 是在调用时
    读这个值的，所以临时改掉即可，不必复制一份合成逻辑。
    """
    from core.tts import synthesize_once

    original = settings.tts_voice
    settings.tts_voice = voice
    try:
        return synthesize_once(text)
    finally:
        settings.tts_voice = original


def _create_voice(url: str, prefix: str, prompt_seconds: float | None = None) -> str | None:
    """创建音色，返回 voice_id；失败返回 None 并打印排查方向。

    Args:
        prompt_seconds: 提示音取多少秒。None = 用 DashScope 默认值（10 秒）。
            样本够长时**调大它**能明显改善相似度 —— 默认只取前 10 秒，
            30 秒的样本会有 2/3 被丢掉。
    """
    from dashscope.audio.tts_v2 import VoiceEnrollmentException

    print(f"创建音色（target_model={settings.tts_model}, prefix={prefix}）…")
    if prompt_seconds:
        print(f"  提示音长度: {prompt_seconds} 秒（默认只取 10 秒）")
    print("  注意：这一步是计费操作")
    try:
        voice_id = _service().create_voice(
            target_model=settings.tts_model,
            prefix=prefix,
            url=url,
            language_hints=["zh"],
            max_prompt_audio_length=prompt_seconds,
        )
    except VoiceEnrollmentException as e:
        print(f"  [FAIL] 创建失败: {e}")
        print("\n  排查方向：")
        print("    - 报 url should start with http/https → 用了本地路径或 oss://")
        print("    - 报下载/拉取失败 → DashScope（北京）访问不到该地址，")
        print("      换个国内可达的直链；确认是**直链**不是网盘分享页")
        print("    - 报音频格式/时长问题 → 见 docs/voice_clone_recording.md 的录制要求")
        print("    - 报权限/未开通 → 控制台开通声音复刻")
        return None
    print(f"  [OK] 音色 ID: {voice_id}")
    return voice_id


def _verify_voice(voice_id: str) -> bool:
    """用复刻出的音色合成一句话，确认真的能出声。"""
    print("用复刻音色合成一句验证…")
    try:
        t0 = time.perf_counter()
        audio = _synth_with_voice("这是用复刻音色合成的验证音频。", voice_id)
    except Exception as e:
        print(f"  [FAIL] 合成失败: {type(e).__name__}: {e}")
        print("  排查：音色可能还在训练中，等几分钟再试")
        return False
    if not audio:
        print("  [FAIL] 合成结果为空")
        return False
    print(f"  [OK] {len(audio)} 字节，耗时 {time.perf_counter() - t0:.2f}s")
    return True


def _delete_voice(voice_id: str) -> None:
    try:
        _service().delete_voice(voice_id)
        print(f"  [OK] 已删除音色 {voice_id}")
    except Exception as e:
        print(f"  [WARN] 删除失败，请手动清理 {voice_id}: {e}")


# ============================================================
# 子命令
# ============================================================


def cmd_check(_args) -> int:
    from dashscope.audio.tts_v2 import VoiceEnrollmentException

    print(f"目标模型: {settings.tts_model}")
    print(f"当前音色: {settings.tts_voice}")
    try:
        voices = _service().list_voices(page_index=0, page_size=50)
    except VoiceEnrollmentException as e:
        print(f"\n[FAIL] 声音复刻服务不可用：{e}")
        return 1

    print(f"\n[OK] 声音复刻服务可用，已有音色 {len(voices)} 个")
    for v in voices:
        print(f"  - {v.get('voice_id')}  ({v.get('status', '?')})")
    return 0


def cmd_dry_run(_args) -> int:
    """
    不碰你的声音、也不创建音色：只验证"合成能出声"这一步。

    ``create_voice`` 那一环没法在这里干跑 —— 它要求公网 http(s) 直链，
    而本地/oss:// 都被实测否决（见本文件顶部）。想完整验证请用::

        python scripts/tts_enroll.py enroll --url <你的直链> --name test --test-only
    """
    print("=" * 60)
    print("干跑：验证本地 TTS 合成链路（不创建音色、不碰你的声音）")
    print("=" * 60)

    print(f"\n[1/2] 用预置音色 {settings.tts_voice} 合成样本…")
    t0 = time.perf_counter()
    try:
        audio = _synth_with_voice(_PROBE_TEXT, settings.tts_voice)
    except Exception as e:
        print(f"  [FAIL] 合成失败: {type(e).__name__}: {e}")
        return 1
    print(f"  [OK] {len(audio)} 字节，耗时 {time.perf_counter() - t0:.2f}s")
    if not audio:
        print("  [FAIL] 样本为空")
        return 1

    print("\n[2/2] 复刻前置条件自检…")
    ok, reason = (True, "已配置") if settings.dashscope_api_key else (False, "缺少 Key")
    print(f"  DashScope Key: {reason}")
    try:
        voices = _service().list_voices(page_index=0, page_size=1)
        print(f"  复刻服务: 可用（当前 {len(voices)} 个音色）")
    except Exception as e:
        print(f"  复刻服务: 不可用 {e}")
        ok = False

    print("\n" + "=" * 60)
    print("本地合成链路正常。下一步：把你的录音放到公网直链，然后跑")
    print("  python scripts/tts_enroll.py enroll --url <直链> --name <前缀> --test-only")
    print("=" * 60)
    return 0 if ok else 1


def cmd_enroll(args) -> int:
    _check_prefix(args.name)
    _check_url(args.url)

    print(f"用 {args.url} 复刻音色（前缀 {args.name}）\n")
    voice_id = _create_voice(args.url, args.name, prompt_seconds=args.prompt_seconds)
    if not voice_id:
        return 1

    verified = _verify_voice(voice_id)

    if args.test_only:
        print("\n--test-only：验证完删除音色…")
        _delete_voice(voice_id)
        if verified:
            print("\n链路可用 —— 去掉 --test-only 再用同样地址正式创建即可。")
            return 0
        return 1

    print(f"\n音色已保留：{voice_id}")
    if verified:
        print(f"启用：python scripts/tts_enroll.py use {voice_id}")
    else:
        print("但用该音色合成失败，先别启用 —— 上面有排查方向。")
        return 1
    return 0


def cmd_list(args) -> int:
    return cmd_check(args)


def _upsert_env(env_path: Path, key: str, value: str) -> None:
    """写入或替换 config/.env 里的某个键。"""
    lines = env_path.read_text(encoding="utf-8").splitlines()
    entry = f"{key}={value}"
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            print(f"  {line}  →  {entry}")
            lines[i] = entry
            break
    else:
        lines.append(entry)
        print(f"  + {entry}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_use(args) -> int:
    """
    把音色（以及它配套的模型）写入 config/.env。

    **必须同时改模型**：音色 ID 里写死了基底模型，合成时用别的模型会返回
    418。所以这里从音色 ID 前缀推断模型，除非用 --model 显式指定。
    """
    env_path = _PROJECT_ROOT / "config" / ".env"
    if not env_path.exists():
        print(f"[FAIL] 找不到 {env_path}")
        return 1

    model = args.model or _infer_model(args.voice_id)
    if not model:
        print(f"[FAIL] 无法从 {args.voice_id} 推断基底模型，请用 --model 指定")
        return 1

    print(f"写入 {env_path}:")
    _upsert_env(env_path, "TTS_MODEL", model)
    _upsert_env(env_path, "TTS_VOICE", args.voice_id)

    print("\n改完要**重启服务**才生效（配置是启动时读入的）")
    print("重启后验证：python scripts/tts_check.py")
    return 0


def cmd_delete(args) -> int:
    from dashscope.audio.tts_v2 import VoiceEnrollmentException

    try:
        _service().delete_voice(args.voice_id)
        print(f"[OK] 已删除音色 {args.voice_id}")
        return 0
    except VoiceEnrollmentException as e:
        print(f"[FAIL] 删除失败: {e}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="声音复刻工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="检查服务可用性与已有音色").set_defaults(fn=cmd_check)
    sub.add_parser("list", help="同 check").set_defaults(fn=cmd_list)
    sub.add_parser("dry-run", help="验证本地合成链路").set_defaults(fn=cmd_dry_run)

    p_enroll = sub.add_parser("enroll", help="用公网直链创建音色")
    p_enroll.add_argument("--url", required=True, help="录音的公网 http(s) 直链")
    p_enroll.add_argument(
        "--name",
        default="myvoice",
        help="音色前缀：小写字母+数字，<10 位（默认 myvoice）",
    )
    p_enroll.add_argument(
        "--test-only",
        action="store_true",
        help="建完验证通过就删除，用来测地址能不能用",
    )
    p_enroll.add_argument(
        "--prompt-seconds",
        type=float,
        default=None,
        help="提示音取多少秒（默认只取 10 秒）。样本够长时调大能明显改善相似度 —— "
        "比如录了 40 秒，默认会有 30 秒被丢掉。建议设成样本时长的 70%% 左右。",
    )
    p_enroll.set_defaults(fn=cmd_enroll)

    p_use = sub.add_parser("use", help="把音色+配套模型写入 config/.env")
    p_use.add_argument("voice_id")
    p_use.add_argument(
        "--model",
        default=None,
        help="合成模型；默认从音色 ID 前缀推断（如 cosyvoice-v3.5-flash-bailian-xxx → cosyvoice-v3.5-flash）",
    )
    p_use.set_defaults(fn=cmd_use)

    p_del = sub.add_parser("delete", help="删除音色")
    p_del.add_argument("voice_id")
    p_del.set_defaults(fn=cmd_delete)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
