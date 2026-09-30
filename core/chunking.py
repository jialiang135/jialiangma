"""
知识库切块配置（领域逻辑）
==========================

回答一个问题：**"这个知识库该怎么切块"**。是切块参数的唯一真相源 ——
默认值、合法区间、校验、持久化读写都收在这里，入库与重建只从这一个口子取。

背景：切块参数原先散落在 ``config/settings.py`` 里全局写死
（``chunk_size=1000`` / ``chunk_overlap=200`` / ``use_semantic_splitter=True``），
所有知识库只能共用一套、改一次要重启进程。现在按知识库（owner）可配，
对齐 Dify 的"数据集 → 自动分段 / 自定义分段"。

与"结构感知"的既有语义保持一致（见 ``rag/text_splitter.py`` 模块 docstring）：
``chunk_overlap`` **只对定长路径生效**，结构感知路径刻意不加重叠。所以
``mode=semantic`` 时前端把 size/overlap 置灰并说明"按文档结构切，不加重叠"
—— 不是禁用功能，而是如实反映切块器本身的设计取舍。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from config.settings import settings

# ============================================================
# 取值
# ============================================================

MODE_SEMANTIC = "semantic"  # 结构感知：Markdown 标题 / 中文编号 / 段落 → 定长兜底
MODE_FIXED = "fixed"  # 纯定长（RecursiveCharacterTextSplitter）
VALID_MODES = (MODE_SEMANTIC, MODE_FIXED)

MODE_LABELS = {
    MODE_SEMANTIC: "结构感知",
    MODE_FIXED: "定长切分",
}

# chunk_size 上下界存在的意义：下界挡住"填 10 把文档切成碎片"（每块就几个字，
# 检索片段毫无上下文）；上界挡住"填 100000 等于不切"（单块远超 embedding 与
# 上下文预算，反而什么都检索不准）。默认 1000 落在这个区间的中部。
CHUNK_SIZE_MIN = 100
CHUNK_SIZE_MAX = 8000

# overlap 只要求非负且 < chunk_size（等于 chunk_size 会让切分原地打转/死循环）。
CHUNK_OVERLAP_MIN = 0

# 自定义分隔符数量上限，防止把一长串字符塞进来拖慢切分。
MAX_SEPARATORS = 20

#: 改配置的生效口径 —— 前端直接展示这段，免得用户以为"点保存就立刻重切"。
APPLIES_TO_NOTE = (
    "配置对「之后上传的新文件」立即生效；对已经入库的文件，"
    "需要点下方的「重建向量索引」才会按新配置重新切块。"
)


@dataclass(frozen=True)
class ChunkingConfig:
    """
    一套切块配置。``frozen=True`` 是刻意的：配置对象在入库/重建的调用链里
    传递，若可变就可能被某处意外改动，导致同一批文件用两套参数切。
    """

    mode: str
    chunk_size: int
    chunk_overlap: int
    separators: list[str] | None = None

    @property
    def use_semantic_splitter(self) -> bool:
        """给 ``rag.text_splitter`` 用的布尔开关（历史参数名，别在本层外传播）。"""
        return self.mode == MODE_SEMANTIC

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "separators": list(self.separators) if self.separators else None,
        }


def default_chunking_config() -> ChunkingConfig:
    """
    默认配置 —— **从全局 settings 派生，等价于改造前的行为**。

    这是"默认行为完全一致"的关键：没有单独配过切块参数的知识库，读出来的
    就是 settings 里那几个值，切块结果与改造前逐字相同。刻意不在这里写死
    常量，否则以后改 settings 会出现"默认值"有两份、互相对不上。
    """
    return ChunkingConfig(
        mode=MODE_SEMANTIC if settings.use_semantic_splitter else MODE_FIXED,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=None,
    )


def chunking_bounds() -> dict:
    """合法区间（供前端约束输入、也用于接口自描述）。"""
    return {
        "chunk_size_min": CHUNK_SIZE_MIN,
        "chunk_size_max": CHUNK_SIZE_MAX,
        "chunk_overlap_min": CHUNK_OVERLAP_MIN,
        "max_separators": MAX_SEPARATORS,
    }


# ============================================================
# 校验
# ============================================================


def _coerce_int(value, field: str) -> int:
    """把输入转成 int；bool 与不可转的值一律判非法（bool 是 int 子类，要单独挡）。"""
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ValueError(f"{field} 必须是整数，收到 {value!r}")


def _normalize_separators(value) -> list[str] | None:
    """
    规整自定义分隔符。

    接受字符串数组，也接受"逗号分隔的字符串"（前端单行输入框的折中）；
    支持 ``\\n`` / ``\\t`` / ``\\r`` 转义写法。空串与空列表都视为"用内置默认"。
    """
    if value is None:
        return None
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        raise ValueError("separators 必须是字符串数组或逗号分隔的字符串")

    cleaned: list[str] = []
    for part in parts:
        if not isinstance(part, str):
            raise ValueError("separators 的每一项都必须是字符串")
        part = part.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
        if part != "":
            cleaned.append(part)
    if not cleaned:
        return None
    if len(cleaned) > MAX_SEPARATORS:
        raise ValueError(f"自定义分隔符最多 {MAX_SEPARATORS} 个（收到 {len(cleaned)}）")
    return cleaned


def validate_chunking_payload(payload: dict, base: ChunkingConfig | None = None) -> ChunkingConfig:
    """
    校验并合并一份切块配置，返回不可变的 ``ChunkingConfig``。

    采用"在 base 之上做部分覆盖"的语义：请求只带想改的字段即可，缺省字段沿用
    当前值（``base``，默认取全局默认）。校验失败抛 ``ValueError``，消息直接面向
    用户（路由层把它转成 400 的 detail，所以必须是人话，不是堆栈）。
    """
    base = base or default_chunking_config()

    mode = payload.get("mode", base.mode)
    if not isinstance(mode, str) or mode not in VALID_MODES:
        raise ValueError(
            f"mode 只能是 {MODE_SEMANTIC}（结构感知）或 {MODE_FIXED}（定长），收到 {mode!r}"
        )

    chunk_size = _coerce_int(payload.get("chunk_size", base.chunk_size), "chunk_size")
    if not (CHUNK_SIZE_MIN <= chunk_size <= CHUNK_SIZE_MAX):
        raise ValueError(
            f"chunk_size 必须在 {CHUNK_SIZE_MIN}~{CHUNK_SIZE_MAX} 之间（收到 {chunk_size}）："
            f"过小会把文档切成没有上下文的碎片，过大则单块超出检索与上下文预算"
        )

    chunk_overlap = _coerce_int(payload.get("chunk_overlap", base.chunk_overlap), "chunk_overlap")
    if chunk_overlap < CHUNK_OVERLAP_MIN:
        raise ValueError(f"chunk_overlap 不能小于 {CHUNK_OVERLAP_MIN}（收到 {chunk_overlap}）")
    if chunk_overlap >= chunk_size:
        raise ValueError(
            f"chunk_overlap 必须小于 chunk_size（收到 overlap={chunk_overlap} >= size={chunk_size}）"
        )

    separators = _normalize_separators(payload.get("separators", base.separators))
    return ChunkingConfig(
        mode=mode,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=separators,
    )


# ============================================================
# 持久化读写
# ============================================================


def _config_from_row(row: dict) -> ChunkingConfig:
    """从数据库行还原配置；行里的脏值不信任，一律再过一遍校验。"""
    return validate_chunking_payload(row)


async def load_chunking_config(owner_id: int) -> ChunkingConfig:
    """
    异步读取某知识库的切块配置（供请求路径 await）。

    没配置过 → 返回默认（等价改造前行为）；行内容非法 → 记告警并用默认值，
    绝不让一条脏数据把知识库读取整个打挂。
    """
    from core.db.chunk_config import get_chunk_config

    row = await get_chunk_config(owner_id)
    if not row:
        return default_chunking_config()
    try:
        return _config_from_row(row)
    except ValueError as e:
        logger.warning("知识库 #{} 的切块配置非法，回落到默认: {}", owner_id, e)
        return default_chunking_config()


async def save_chunking_config(owner_id: int, config: ChunkingConfig) -> None:
    """异步写入某知识库的切块配置（供请求路径 await）。"""
    from core.db.chunk_config import upsert_chunk_config

    await upsert_chunk_config(
        owner_id,
        mode=config.mode,
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
        separators=config.separators,
    )


def resolve_chunking_config(owner_id: int) -> ChunkingConfig:
    """
    **同步**读取某知识库的切块配置 —— 供工作线程里的入库 / 重建调用。

    为什么是同步：入库与重建跑在线程池的工作线程里（见 ``core/kb_tasks`` 顶部），
    那里没有 event loop，只能通过 ``run_async_from_thread`` 把异步 DB 调用提交回
    主循环。主循环未就绪、表异常等任何读取失败都**回落到默认配置**，而不是把
    文档入库整个搞失败 —— 配置读不到只是"用了默认值"，默认值正是改造前的行为。
    """
    from core.db.engine import run_async_from_thread

    coro = load_chunking_config(owner_id)
    try:
        config = run_async_from_thread(coro)
    except Exception as e:
        # 刻意宽 except：任何读取失败都回落到默认，不让配置读取拖垮文档入库。
        # 失败时协程可能**根本没被提交**（例如主循环未就绪），显式 close 掉，
        # 否则运行时会报 "coroutine ... was never awaited" 噪音警告。
        coro.close()
        logger.warning("读取知识库 #{} 切块配置失败，本次使用默认配置: {}", owner_id, e)
        return default_chunking_config()
    return config if isinstance(config, ChunkingConfig) else default_chunking_config()


def chunking_description(config: ChunkingConfig) -> dict:
    """
    接口自描述：配置本体 + 默认值 + 合法区间 + 生效口径。

    一次性给全，是为了让前端**不用再抄一份**规则（本项目已有的教训：
    ``AuthRequirements`` 就是为了消灭"前后端各写一套校验、日后漂移"）。
    """
    return {
        **config.to_dict(),
        "defaults": default_chunking_config().to_dict(),
        "mode_labels": MODE_LABELS,
        "bounds": chunking_bounds(),
        "applies_to": APPLIES_TO_NOTE,
    }
