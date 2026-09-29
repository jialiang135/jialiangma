"""
多格式文档加载器
支持 PDF/Word/Excel/TXT/Markdown/图片(OCR)/ZIP压缩包

失败原因要「可分辨」
--------------------
早期实现里 ``load_image_ocr`` 失败只返回 ``""``，上层再统一报
"无法解析文件内容"。于是**三种完全不同的原因**——没装 OCR 引擎、
图片/扫描件里没有文字、文件本来就是空的——给用户的是同一句话，
排查时无从下手（这正是"静默失效"）。

现在加载失败一律抛 ``DocumentLoadError`` 子类，并带机器可读的 ``reason``；
``load_document_detailed`` 把每次加载归类为 ``ok`` / ``failed`` / ``empty``，
供上层如实写进任务状态与日志。``load_single_document`` /
``load_documents_from_paths`` 的对外行为保持兼容。
"""

import os
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

# ========================================
# 机器可读的失败原因码
# ========================================
REASON_OK = "ok"
REASON_FILE_MISSING = "file_missing"
REASON_UNSUPPORTED_FORMAT = "unsupported_format"
REASON_OCR_ENGINE_MISSING = "ocr_engine_missing"
REASON_OCR_NO_TEXT = "ocr_no_text"
REASON_EMPTY_CONTENT = "empty_content"
REASON_ZIP_LIMIT = "zip_limit_exceeded"
REASON_PARSE_ERROR = "parse_error"

#: 原因码 → 中文短标签，给任务状态 / 日志用（别在调用方各写一份、漂移）。
REASON_LABELS = {
    REASON_OK: "正常",
    REASON_FILE_MISSING: "源文件不存在",
    REASON_UNSUPPORTED_FORMAT: "不支持的文件格式",
    REASON_OCR_ENGINE_MISSING: "缺少 OCR 引擎（Tesseract 未安装）",
    REASON_OCR_NO_TEXT: "图片中未识别出文字",
    REASON_EMPTY_CONTENT: "文件内容为空",
    REASON_ZIP_LIMIT: "压缩包解压后超限，已拒绝",
    REASON_PARSE_ERROR: "解析失败",
}

#: 走 OCR 的扩展名 —— 用来把"空结果"解释成"图片没识别出文字"。
OCR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff"}


class DocumentLoadError(Exception):
    """
    文档加载失败。

    ``reason`` 是机器可读的原因码（见上面的 ``REASON_*``），
    上层据此把"失败"与"内容为空"分开处理。
    """

    reason = REASON_PARSE_ERROR

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UnsupportedFormatError(DocumentLoadError, ValueError):
    """扩展名没有对应加载器。继承 ValueError 以保持历史行为。"""

    reason = REASON_UNSUPPORTED_FORMAT


class MissingOcrEngineError(DocumentLoadError):
    """OCR 引擎（Tesseract）未安装或不可用。"""

    reason = REASON_OCR_ENGINE_MISSING


class ZipLimitExceededError(DocumentLoadError):
    """ZIP 解压后大小 / 成员数超限 —— 疑似压缩炸弹，直接拒绝。"""

    reason = REASON_ZIP_LIMIT


@dataclass
class LoadOutcome:
    """
    单文件加载结果。把"内容"与"为什么没有内容"一起返回，避免信息在中途丢失。

    ``status``:
        - ``"ok"``     —— 成功提取到非空文本；
        - ``"empty"``  —— 解析成功但没有文本（空文件 / 扫描件无文字层 / 图片没识别出字）；
        - ``"failed"`` —— 加载失败（引擎缺失、格式不支持、源文件不存在、解析异常、超限）。
    """

    filepath: str
    filename: str
    content: str = ""
    status: str = "ok"
    reason: str = REASON_OK
    detail: str = ""


def _empty_reason(ext: str) -> tuple[str, str]:
    """按扩展名把"内容为空"解释成更具体的原因（原因码, 人话说明）。"""
    if ext in OCR_EXTENSIONS:
        return REASON_OCR_NO_TEXT, "图片中未识别出文字（可能是扫描质量差，或图片本身无文字）"
    if ext == ".zip":
        return REASON_EMPTY_CONTENT, "压缩包内未提取到任何文本"
    if ext in {".pdf", ".doc", ".docx"}:
        return REASON_EMPTY_CONTENT, "未提取到文本（若为扫描件/纯图片版 PDF，需 OCR 才能取字）"
    return REASON_EMPTY_CONTENT, "文件内容为空"


def load_pdf(filepath: str) -> str:
    """加载 PDF 文件文本"""
    try:
        from pypdf import PdfReader

        reader = PdfReader(filepath)
        texts = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                texts.append(text)
        content = "\n\n".join(texts)
        logger.info(f"PDF 解析完成: {filepath}, {len(reader.pages)} 页, {len(content)} 字符")
        return content
    except Exception as e:
        logger.error(f"PDF 解析失败: {filepath} - {e}")
        raise


def load_docx(filepath: str) -> str:
    """加载 Word 文档文本"""
    try:
        from docx import Document

        doc = Document(filepath)
        texts = [para.text for para in doc.paragraphs if para.text.strip()]
        content = "\n\n".join(texts)
        logger.info(f"DOCX 解析完成: {filepath}, {len(texts)} 段落")
        return content
    except Exception as e:
        logger.error(f"DOCX 解析失败: {filepath} - {e}")
        raise


def load_xlsx(filepath: str) -> str:
    """加载 Excel 文件文本（逐行拼接）"""
    try:
        import openpyxl

        # 使用 with 语句确保资源正确释放
        with openpyxl.load_workbook(filepath, read_only=True, data_only=True) as wb:
            all_texts = []
            sheet_names = list(wb.sheetnames)  # 在 close 前获取
            for sheet_name in sheet_names:
                ws = wb[sheet_name]
                sheet_texts = [f"--- Sheet: {sheet_name} ---"]
                for row in ws.iter_rows(values_only=True):
                    row_text = " | ".join(str(cell) for cell in row if cell is not None)
                    if row_text.strip():
                        sheet_texts.append(row_text)
                all_texts.append("\n".join(sheet_texts))
        content = "\n\n".join(all_texts)
        logger.info(f"XLSX 解析完成: {filepath}, sheets={sheet_names}")
        return content
    except Exception as e:
        logger.error(f"XLSX 解析失败: {filepath} - {e}")
        raise


def load_txt_md(filepath: str) -> str:
    """加载纯文本 / Markdown 文件"""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"文件不存在: {filepath}")

    encodings = ["utf-8", "gbk", "gb2312", "latin-1"]
    for enc in encodings:
        try:
            with open(filepath, encoding=enc) as f:
                content = f.read()
            logger.info(f"TXT/MD 解析完成: {filepath}, 编码={enc}, {len(content)} 字符")
            return content
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法解码文件: {filepath}")


def load_image_ocr(filepath: str) -> str:
    """
    对图片进行 OCR 文字提取（需要 Tesseract 已安装）。

    Raises:
        MissingOcrEngineError: pytesseract 未安装，或 Tesseract 二进制找不到。
        DocumentLoadError:     其它 OCR 失败（损坏图片等）。

    注意：OCR **成功但没识别出文字**时返回 ``""``（而非抛错）——
    那是"图片里确实没字"，与"引擎没装"是两回事，由 ``load_document_detailed``
    归类为 ``empty`` / ``ocr_no_text``。
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError as e:
        raise MissingOcrEngineError(f"缺少 OCR 依赖（pytesseract/Pillow）: {e}") from e

    try:
        with Image.open(filepath) as image:
            text = pytesseract.image_to_string(image, lang="chi_sim+eng")
    except Exception as e:
        # 具体区分"引擎没装"：pytesseract 抛 TesseractNotFoundError（且消息含 tesseract），
        # 老版本/异常路径下只能靠消息文本判断。
        not_found = getattr(pytesseract, "TesseractNotFoundError", None)
        if (isinstance(not_found, type) and isinstance(e, not_found)) or "tesseract" in str(
            e
        ).lower():
            raise MissingOcrEngineError("缺少 OCR 引擎：未安装 Tesseract 或它不在 PATH 中") from e
        raise DocumentLoadError(f"图片 OCR 失败: {e}") from e

    logger.info(f"图片 OCR 完成: {filepath}, {len(text)} 字符")
    return text


# ========================================
# ZIP 解压上限（防压缩炸弹）
# ========================================
# 依据：
#   - 上传入口已把**压缩包本身**限制为 settings.max_upload_size_mb（默认 50MB）。
#   - 但 DEFLATE 的理论膨胀比高达 ~1032:1，50MB 的炸弹解压后能到 ~50GB，
#     足以撑爆磁盘 —— 上传限制拦不住"解压"这一步。
#   - 真实文档压缩包（PDF/DOCX/图片本身已压缩）膨胀比通常 1~3 倍，纯文本
#     一般不超过 10 倍。取 **20 倍**：50MB → 1GB，留足余量，同时把千倍的
#     炸弹挡在门外。默认值即"与上传限制挂钩的合理倍数"，可调。
#   - 成员数上限是第二道闸：防"海量小文件"（每个都不大，但几万个）。
ZIP_MAX_EXPANSION_FACTOR = 20
ZIP_MAX_MEMBERS = 2000


def _zip_limits() -> tuple[int, int]:
    """返回 ``(解压后总字节上限, 成员数上限)``。"""
    # 延迟导入 settings：避免加载器在导入期就绑定配置（便于测试替换）。
    from config.settings import settings

    base = max(int(settings.max_upload_size_mb), 1) * 1024 * 1024
    return base * ZIP_MAX_EXPANSION_FACTOR, ZIP_MAX_MEMBERS


def load_zip(filepath: str, upload_dir: str) -> list[str]:
    """
    解压 ZIP 文件，递归提取其中支持的文档文本。
    返回所有提取到的文本片段列表。

    Raises:
        ZipLimitExceededError: 解压后总大小或成员数超限（疑似压缩炸弹）。
    """
    texts = []
    with zipfile.ZipFile(filepath, "r") as zf:
        _check_zip_limits(zf.infolist())

        # 创建临时目录解压
        extract_dir = tempfile.mkdtemp(prefix="zip_extract_")
        try:
            zf.extractall(extract_dir)

            for root, _dirs, files in os.walk(extract_dir):
                for fname in files:
                    full_path = os.path.join(root, fname)
                    try:
                        file_text = load_single_document(full_path, upload_dir)
                        if file_text.strip():
                            texts.append(file_text)
                    except Exception as e:
                        logger.warning(f"ZIP 内文件解析跳过: {fname} - {e}")
        finally:
            # 清理临时目录
            import shutil

            shutil.rmtree(extract_dir, ignore_errors=True)

    combined = "\n\n".join(texts)
    logger.info(f"ZIP 解析完成: {filepath}, 提取 {len(texts)} 个文本片段")
    return [combined] if combined.strip() else []


def _check_zip_limits(infos: list[zipfile.ZipInfo]) -> None:
    """
    解压**之前**按中央目录里声明的成员大小 / 数量做校验，超限直接拒绝。

    用 ``info.file_size``（声明值）而非实际解压后统计：目的是"解压前"就拦下来，
    不能先把炸弹写满磁盘再判断。声明值被伪造的极端场景由 20 倍余量兜底。
    """
    max_total, max_members = _zip_limits()

    if len(infos) > max_members:
        raise ZipLimitExceededError(
            f"压缩包成员数超限：{len(infos)} 个 > 上限 {max_members} 个，已拒绝解压"
        )

    total = sum(info.file_size for info in infos)
    if total > max_total:
        raise ZipLimitExceededError(
            f"压缩包解压后大小超限：约 {total / 1024 / 1024:.0f}MB > 上限 "
            f"{max_total / 1024 / 1024:.0f}MB（上传上限的 {ZIP_MAX_EXPANSION_FACTOR} 倍），"
            f"疑似压缩炸弹，已拒绝解压"
        )
    logger.debug(
        f"ZIP 校验通过: {len(infos)} 个成员, 解压后约 {total / 1024 / 1024:.1f}MB "
        f"(上限 {max_total / 1024 / 1024:.0f}MB / {max_members} 个)"
    )


# ========================================
# 统一入口
# ========================================

SUPPORTED_EXTENSIONS = {
    ".pdf": load_pdf,
    ".docx": load_docx,
    ".doc": load_docx,
    ".xlsx": load_xlsx,
    ".xls": load_xlsx,
    ".txt": load_txt_md,
    ".md": load_txt_md,
    ".py": load_txt_md,
    ".json": load_txt_md,
    ".csv": load_txt_md,
    ".png": load_image_ocr,
    ".jpg": load_image_ocr,
    ".jpeg": load_image_ocr,
    ".bmp": load_image_ocr,
    ".tiff": load_image_ocr,
}


def load_single_document(filepath: str, upload_dir: str = "") -> str:
    """
    根据文件扩展名选择合适的加载器，返回提取的文本内容。

    Raises:
        UnsupportedFormatError: 扩展名无对应加载器（``ValueError`` 子类，兼容旧行为）。
        其它 ``DocumentLoadError`` / 加载器自身异常：向上抛出，由调用方决定如何处理。
    """
    ext = Path(filepath).suffix.lower()

    if ext == ".zip":
        texts = load_zip(filepath, upload_dir or os.path.dirname(filepath))
        return texts[0] if texts else ""

    loader = SUPPORTED_EXTENSIONS.get(ext)
    if loader is None:
        raise UnsupportedFormatError(
            f"不支持的文件格式: {ext}。支持的格式: {list(SUPPORTED_EXTENSIONS.keys())}"
        )

    return loader(filepath)


def load_document_detailed(filepath: str, upload_dir: str = "") -> LoadOutcome:
    """
    加载单文件并返回**带原因**的结果（``ok`` / ``empty`` / ``failed``）。

    与 ``load_single_document`` 的区别：后者失败只会抛异常或返回空串，
    调用方拿不到"为什么"；这里把失败原因收进 ``LoadOutcome``，供上层
    如实写进任务状态与日志。**不抛异常**（除非编程错误）。
    """
    name = Path(filepath).name
    ext = Path(filepath).suffix.lower()

    if not os.path.exists(filepath):
        return LoadOutcome(
            filepath,
            name,
            status="failed",
            reason=REASON_FILE_MISSING,
            detail="源文件不存在（可能已被删除）",
        )
    try:
        if ext == ".zip":
            texts = load_zip(filepath, upload_dir or os.path.dirname(filepath))
            content = texts[0] if texts else ""
        else:
            loader = SUPPORTED_EXTENSIONS.get(ext)
            if loader is None:
                raise UnsupportedFormatError(f"不支持的文件格式: {ext}")
            content = loader(filepath)
    except MissingOcrEngineError as e:
        return LoadOutcome(
            filepath, name, status="failed", reason=REASON_OCR_ENGINE_MISSING, detail=e.message
        )
    except ZipLimitExceededError as e:
        return LoadOutcome(
            filepath, name, status="failed", reason=REASON_ZIP_LIMIT, detail=e.message
        )
    except UnsupportedFormatError as e:
        return LoadOutcome(
            filepath, name, status="failed", reason=REASON_UNSUPPORTED_FORMAT, detail=str(e)
        )
    except DocumentLoadError as e:
        return LoadOutcome(filepath, name, status="failed", reason=e.reason, detail=e.message)
    except Exception as e:
        return LoadOutcome(
            filepath, name, status="failed", reason=REASON_PARSE_ERROR, detail=f"解析异常: {e}"
        )

    if not content.strip():
        reason, detail = _empty_reason(ext)
        return LoadOutcome(filepath, name, status="empty", reason=reason, detail=detail)

    return LoadOutcome(filepath, name, content=content, status="ok", reason=REASON_OK)


def load_documents_from_paths(filepaths: list[str], upload_dir: str = "") -> list[dict]:
    """
    批量加载文档，返回列表 [{"filepath": str, "filename": str, "content": str}, ...]

    只保留成功提取到非空文本的文档；失败 / 为空的会在日志里**带原因**记一条。
    需要拿到每个文件的具体原因时用 ``load_document_detailed``。
    """
    results = []
    for fp in filepaths:
        outcome = load_document_detailed(fp, upload_dir)
        if outcome.status == "ok":
            results.append(
                {
                    "filepath": outcome.filepath,
                    "filename": outcome.filename,
                    "content": outcome.content,
                }
            )
        else:
            label = REASON_LABELS.get(outcome.reason, outcome.reason)
            logger.warning(
                f"文档未加载({outcome.status}): {outcome.filename} - {outcome.detail} [{label}]"
            )
    return results
