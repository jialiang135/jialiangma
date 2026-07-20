"""
文本清洗、语义分块工具
=====================
提供从原始文档到可索引文本块的完整处理流水线：

1. ``clean_text()`` — 清洗空字节 / 控制字符 / 多余空白
2. 分块策略（按优先级）：
   a. **标题层级分块** —— Markdown 标题 (``#``/``##``/``###``) 或中文编号章节
   b. **段落分块** —— 双换行符
   c. **定长递归分块** —— ``RecursiveCharacterTextSplitter``（兜底）
3. ``SemanticTextSplitter`` — 自动按结构感知策略分块
4. ``process_document()`` / ``process_documents_batch()`` — 完整流水线
"""
import re
from typing import Optional

from langchain_text_splitters import RecursiveCharacterTextSplitter
from loguru import logger


# ====================================================================
# 清洗
# ====================================================================

def clean_text(text: str) -> str:
    """
    清洗文本：
    - 移除 NULL 字节
    - 规范化空白字符
    - 过滤无意义短行
    - 合并多余换行
    """
    if not text:
        return ""

    # 移除 NULL 字节
    text = text.replace("\x00", "")

    # 移除奇怪的 Unicode 控制字符
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', "", text)

    # 规范化空白：多个空格/制表符 → 单个空格
    text = re.sub(r"[ \t]+", " ", text)

    # 规范化换行：超过 3 个连续换行 → 2 个换行
    text = re.sub(r"\n{4,}", "\n\n\n", text)

    # 移除纯空白行在段首段尾
    text = text.strip()

    return text


# ====================================================================
# 过滤 & 去重
# ====================================================================

def filter_short_chunks(chunks: list[str], min_length: int = 20) -> list[str]:
    """过滤掉太短的文本块（无意义碎片）。"""
    return [c for c in chunks if len(c.strip()) >= min_length]


def deduplicate_chunks(chunks: list[str], threshold: float = 0.9) -> list[str]:
    """
    去重：移除高度相似的文本块。

    当前策略：完全相同的内容直接去重；未来可引入 fuzzy matching。
    """
    seen = set()
    unique = []
    for chunk in chunks:
        normalized = chunk.strip()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(chunk)
    if len(chunks) != len(unique):
        logger.info(f"文本去重: {len(chunks)} → {len(unique)} 块")
    return unique


# ====================================================================
# 结构感知分块函数
# ====================================================================

def split_by_headings(text: str) -> list[str]:
    """
    按 Markdown 标题分块 (``#``, ``##``, ``###``, ...)。

    每个标题行与其后续内容组成一个块。标题行被保留在块的开头，
    使得下游能根据标题判断上下文。

    Args:
        text: 原始文本。

    Returns:
        分块列表；若未检测到任何标题则返回 ``[text]``。
    """
    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []

    heading_pattern = re.compile(r"^#{1,6}\s+\S")

    for line in lines:
        if heading_pattern.match(line):
            if current:
                chunks.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)

    if current:
        chunks.append("\n".join(current).strip())

    # 过滤空块
    chunks = [c for c in chunks if c]

    if len(chunks) == 1:
        logger.debug("未检测到 Markdown 标题，返回原文")
    else:
        logger.debug("按 Markdown 标题分块: {} 块", len(chunks))

    return chunks


def split_by_numbered_sections(text: str) -> list[str]:
    """
    按中文编号章节分块。

    支持的章节标题模式（按优先级）:
    1.  ``第[一二三四五六七八九十百千]+[章节篇部]``  — 第一章 / 第二节 / 第三篇
    2.  ``[一二三四五六七八九十]+[、．.]``          — 一、 / 二． / 三.
    3.  ``\\d+\\.``                                 — 1. / 2. / 10.

    每个标题行与其后续内容组成一个块。

    Args:
        text: 原始文本。

    Returns:
        分块列表；若未检测到任何章节标题则返回 ``[text]``。
    """
    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []

    # 组合模式：匹配行首的章节标题
    section_pattern = re.compile(
        r"^\s*第[一二三四五六七八九十百千0-9]+[章节篇部节课]"
        r"|^\s*[一二三四五六七八九十]+[、．.]"
        r"|^\s*\d+\.[\s\t]"
    )

    for line in lines:
        stripped = line.strip()
        if stripped and section_pattern.match(stripped):
            if current:
                chunks.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)

    if current:
        chunks.append("\n".join(current).strip())

    chunks = [c for c in chunks if c]

    if len(chunks) == 1:
        logger.debug("未检测到中文章节标题，返回原文")
    else:
        logger.debug("按中文章节分块: {} 块", len(chunks))

    return chunks


def split_by_paragraphs(text: str) -> list[str]:
    """
    按段落分块（双换行符 ``\\n\\n`` 或 ``\\r\\n\\r\\n``）。

    连续空行被合并为单一分隔符。短段落不会被过滤（由调用方决定）。

    Args:
        text: 原始文本。

    Returns:
        段落列表。若只有一个段落则返回 ``[text]``。
    """
    # 先规范化换行
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = re.split(r"\n\n+", normalized)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    if not paragraphs:
        return [text]

    logger.debug("按段落分块: {} 块", len(paragraphs))
    return paragraphs


# ====================================================================
# SemanticTextSplitter
# ====================================================================

class SemanticTextSplitter:
    """
    语义分块器 —— 按文档结构层次分块。

    分块优先级:
    1.  **Markdown 标题** —— ``#`` / ``##`` / ``###`` 等
    2.  **中文编号章节** —— ``一、`` / ``第一章`` / ``1.`` 等
    3.  **段落** —— 双换行符 ``\\n\\n``
    4.  **定长递归分块** —— 兜底，使用 ``RecursiveCharacterTextSplitter``

    当高层级策略产生 1 个块时自动降级；若块内容超出 ``chunk_size`` 则对该块
    进一步按低层级策略切分。

    Args:
        chunk_size:      定长兜底时的目标块大小（字符数）。
        chunk_overlap:   定长兜底时的块间重叠字符数。
        min_chunk_length: 过滤短块的最小字符数。
    """

    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        min_chunk_length: int = 20,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_length = min_chunk_length

    # ------------------------------------------------------------------
    def split_text(self, text: str) -> list[str]:
        """
        对输入文本执行语义分块，返回最终文本块列表。
        """
        if not text or not text.strip():
            return []

        # ── 1. Markdown 标题 ──
        chunks = split_by_headings(text)
        if len(chunks) > 1:
            logger.info("语义分块: 按 Markdown 标题 → {} 块", len(chunks))
            return self._post_process(self._refine_oversized(chunks))

        # ── 2. 中文编号章节 ──
        chunks = split_by_numbered_sections(text)
        if len(chunks) > 1:
            logger.info("语义分块: 按中文章节 → {} 块", len(chunks))
            return self._post_process(self._refine_oversized(chunks))

        # ── 3. 段落 ──
        chunks = split_by_paragraphs(text)
        if len(chunks) > 1:
            logger.info("语义分块: 按段落 → {} 块", len(chunks))
            return self._post_process(self._refine_oversized(chunks))

        # ── 4. 定长兜底 ──
        logger.info("语义分块: 无结构信息，使用定长分块 (size={})", self.chunk_size)
        splitter = create_text_splitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
        )
        chunks = splitter.split_text(text)
        return self._post_process(chunks)

    # ------------------------------------------------------------------
    def _refine_oversized(self, chunks: list[str]) -> list[str]:
        """
        对超出 ``chunk_size * 1.5`` 的大块进行二次切分（段落 → 定长）。
        小块保持不变以保留语义边界。
        """
        refined: list[str] = []
        threshold = self.chunk_size * 1.5

        for chunk in chunks:
            if len(chunk) > threshold:
                # 先尝试段落切分
                paragraphs = split_by_paragraphs(chunk)
                if len(paragraphs) > 1:
                    # 对每个段落再检查大小
                    for para in paragraphs:
                        if len(para) > threshold:
                            splitter = create_text_splitter(
                                chunk_size=self.chunk_size,
                                chunk_overlap=self.chunk_overlap,
                            )
                            refined.extend(splitter.split_text(para))
                        else:
                            refined.append(para)
                else:
                    splitter = create_text_splitter(
                        chunk_size=self.chunk_size,
                        chunk_overlap=self.chunk_overlap,
                    )
                    refined.extend(splitter.split_text(chunk))
            else:
                refined.append(chunk)

        logger.debug("大块二次切分: {} → {} 块", len(chunks), len(refined))
        return refined

    # ------------------------------------------------------------------
    def _post_process(self, chunks: list[str]) -> list[str]:
        """统一的过滤 + 去重后处理。"""
        chunks = filter_short_chunks(chunks, min_length=self.min_chunk_length)
        chunks = deduplicate_chunks(chunks)
        return chunks


# ====================================================================
# 原有接口（完全向后兼容）
# ====================================================================

def create_text_splitter(
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
    separators: Optional[list[str]] = None,
) -> RecursiveCharacterTextSplitter:
    """
    创建语义分块器。

    使用递归字符分割，优先在段落、句子边界切分。

    Args:
        chunk_size:    目标块大小（字符数）。
        chunk_overlap: 块间重叠字符数。
        separators:    自定义分隔符优先级列表。为 ``None`` 时使用默认值。

    Returns:
        ``RecursiveCharacterTextSplitter`` 实例。
    """
    if separators is None:
        separators = [
            "\n\n\n",   # 大段落分隔
            "\n\n",     # 段落分隔
            "\n",       # 换行
            "。",       # 中文句号
            "！",       # 中文感叹号
            "？",       # 中文问号
            ". ",       # 英文句号
            "! ",       # 英文感叹号
            "? ",       # 英文问号
            "；",       # 中文分号
            "; ",       # 英文分号
            "，",       # 中文逗号
            ", ",       # 英文逗号
            " ",        # 空格
            "",         # 最终兜底：逐字符
        ]

    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=separators,
        keep_separator=True,
        length_function=len,
    )


def process_document(
    content: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
    min_chunk_length: int = 20,
    use_semantic_splitter: bool = False,
) -> list[str]:
    """
    完整的文档处理流程：清洗 → 分块 → 过滤 → 去重。

    Args:
        content:               原始文本内容。
        chunk_size:            定长分块大小（不使用语义分块时生效）。
        chunk_overlap:         定长分块重叠。
        min_chunk_length:      最短块长度。
        use_semantic_splitter: 是否使用 ``SemanticTextSplitter``（按结构分块）。

    Returns:
        最终的文本块列表。
    """
    # 1. 清洗
    cleaned = clean_text(content)
    if not cleaned:
        logger.warning("文档清洗后内容为空")
        return []

    # 2. 分块
    if use_semantic_splitter:
        splitter = SemanticTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            min_chunk_length=min_chunk_length,
        )
        chunks = splitter.split_text(cleaned)
        logger.info(
            "语义分块完成: {} 块 (chunk_size={}, overlap={})",
            len(chunks), chunk_size, chunk_overlap,
        )
    else:
        splitter = create_text_splitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        chunks = splitter.split_text(cleaned)
        logger.info(
            "递归分块完成: {} 块 (chunk_size={}, overlap={})",
            len(chunks), chunk_size, chunk_overlap,
        )

    # 3. 过滤短块
    chunks = filter_short_chunks(chunks, min_length=min_chunk_length)

    # 4. 去重
    chunks = deduplicate_chunks(chunks)

    logger.info("文档处理最终结果: {} 块", len(chunks))
    return chunks


def process_documents_batch(
    docs: list[dict],
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
    use_semantic_splitter: bool = False,
) -> list[dict]:
    """
    批量处理文档，返回 ``[{"filepath": ..., "filename": ..., "chunks": [...]}, ...]``。

    Args:
        docs:                 文档列表，每项含 ``content`` / ``filepath`` / ``filename``。
        chunk_size:           分块大小。
        chunk_overlap:        分块重叠。
        use_semantic_splitter: 是否使用语义分块器。
    """
    results = []
    for doc in docs:
        chunks = process_document(
            doc["content"],
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            use_semantic_splitter=use_semantic_splitter,
        )
        if chunks:
            results.append({
                "filepath": doc["filepath"],
                "filename": doc["filename"],
                "chunks": chunks,
            })
    return results
