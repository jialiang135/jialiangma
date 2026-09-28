"""
路径安全工具
============

统一处理"用户提供的文件名"与"来自数据库的历史路径"，防止路径穿越。

威胁模型
--------
上传接口的 `filename` 完全由客户端控制。若直接参与
``os.path.join(upload_dir, filename)``，那么 ``../../../../config/settings.py``
这样的名字能让写入落到 upload_dir 之外；更糟的是该路径会被持久化进
``files.filepath``，之后被删除接口 ``os.remove`` 使用 —— 同一个缺口
同时提供了**任意文件写**与**任意文件删**。

因此约定：

- 所有来自客户端的文件名 → 必须先过 :func:`safe_filename`
- 所有来自数据库的文件路径 → 读回使用时必须先过 :func:`ensure_within`

注意 ``os.path.basename`` 不足以解决问题：Linux 上它不把 ``\\`` 视作
分隔符，``os.path.basename("..\\\\..\\\\main.py")`` 会原样返回。
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from loguru import logger

# 文件名中不允许出现的字符：Windows 保留字符 + 路径分隔符 + 控制字符
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Windows 保留设备名（不区分大小写）
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# 净化后文件名的最大长度（字节安全的保守值，给扩展名留余量）
MAX_FILENAME_LENGTH = 200


def safe_filename(filename: str, fallback: str = "unnamed") -> str:
    """
    把客户端提供的文件名净化为**纯文件名**（不含任何目录部分）。

    处理步骤：

    1. NFKC 归一化 —— 防止用全角斜杠 ``／`` 之类的相似字符绕过检查；
    2. 取 basename —— ``/`` 与 ``\\`` **都**当作分隔符处理；
    3. 去掉首尾空白与点 —— 消掉 ``..``、``.``、尾随点等相对路径片段；
    4. 剔除控制字符与 Windows 保留字符；
    5. 规避 Windows 保留设备名（``CON``、``COM1`` 等）；
    6. 限制长度，保留扩展名。

    Args:
        filename: 客户端提供的原始文件名。
        fallback: 净化后为空时返回的兜底名。

    Returns:
        保证不含任何路径分隔符的纯文件名，可安全用于 ``os.path.join``。
    """
    if not filename:
        return fallback

    name = unicodedata.normalize("NFKC", filename)
    # 两种分隔符都当分隔符，只保留最后一段
    name = name.replace("\\", "/").split("/")[-1]
    # 首尾空白与点：`..` → ''，`...` → ''，`file.txt.` → 'file.txt'
    name = name.strip().strip(".")
    # 非法字符替换为下划线
    name = _UNSAFE_CHARS.sub("_", name)
    # 折叠内部空白，避免畸形名字
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        return fallback

    # 保留设备名（Windows 上创建会失败或行为异常）
    # 注意无扩展名时 rpartition 会返回空 stem，需退回整个 name 判断
    stem, dot, suffix = name.rpartition(".")
    base_name = stem if dot else name
    if base_name.upper() in _RESERVED_NAMES:
        name = f"{base_name}_{dot}{suffix}" if dot else f"{base_name}_"

    # 截断时保留扩展名
    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, suffix = name.rpartition(".")
        if dot:
            keep = MAX_FILENAME_LENGTH - len(suffix) - 1
            name = f"{stem[: max(keep, 1)]}.{suffix}"
        else:
            name = name[:MAX_FILENAME_LENGTH]

    return name


def ensure_within(base: str | Path, target: str | Path) -> Path:
    """
    断言 ``target`` 落在 ``base`` 目录内，返回解析后的绝对路径。

    用于校验**来自数据库的历史路径**——早期版本没有净化文件名，
    库里可能已经存着穿越路径，直接 ``os.remove`` 就是任意文件删除。
    新写入的路径也应过一遍，作为纵深防御。

    Args:
        base:   基准目录（如 upload_dir）。
        target: 待校验路径。

    Returns:
        ``target`` 的绝对解析路径。

    Raises:
        ValueError: 路径越界。
    """
    base_resolved = Path(base).resolve()
    target_resolved = Path(target).resolve()

    if target_resolved == base_resolved or base_resolved in target_resolved.parents:
        return target_resolved

    logger.warning(
        "路径越界被拦截: target={} base={}",
        target_resolved,
        base_resolved,
    )
    raise ValueError(f"路径越界: {target_resolved} 不在 {base_resolved} 内")


def safe_join(base: str | Path, filename: str) -> Path:
    """
    净化文件名并拼接到 ``base`` 下，返回绝对路径。

    :func:`safe_filename` 之后再做一次 :func:`ensure_within` 校验，
    作为二次防线（净化逻辑若有遗漏，这里会兜住而不是静默越界）。
    """
    clean = safe_filename(filename)
    return ensure_within(base, Path(base).resolve() / clean)


def remove_within(base: str | Path, target: str | Path) -> bool:
    """
    仅当 ``target`` 位于 ``base`` 内时删除它。

    删除接口统一走这里，避免历史脏数据或手工构造的 DB 记录
    把删除操作变成任意文件删除。

    Returns:
        文件存在且已删除返回 True；越界或不存在返回 False（不抛异常，
        便于调用方按"忽略"处理，同时已记录告警日志）。
    """
    try:
        path = ensure_within(base, target)
    except ValueError:
        return False

    if not path.exists():
        return False
    if path.is_dir():
        logger.warning("拒绝删除目录（仅支持文件）: {}", path)
        return False

    path.unlink()
    return True
