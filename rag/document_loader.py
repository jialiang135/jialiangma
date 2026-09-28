"""
多格式文档加载器
支持 PDF/Word/Excel/TXT/Markdown/图片(OCR)/ZIP压缩包
"""

import os
import tempfile
import zipfile
from pathlib import Path

from loguru import logger


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
    """对图片进行 OCR 文字提取（需要 Tesseract 已安装）"""
    try:
        import pytesseract
        from PIL import Image

        with Image.open(filepath) as image:
            text = pytesseract.image_to_string(image, lang="chi_sim+eng")
        logger.info(f"图片 OCR 完成: {filepath}, {len(text)} 字符")
        return text
    except ImportError:
        logger.warning(f"pytesseract 未安装，跳过OCR: {filepath}")
        return ""
    except Exception as e:
        logger.warning(f"图片 OCR 失败: {filepath} - {e}")
        # 检查是否是 tesseract 未安装
        if "tesseract" in str(e).lower():
            logger.warning("Tesseract OCR 引擎未安装，图片文字提取已跳过")
        return ""


def load_zip(filepath: str, upload_dir: str) -> list[str]:
    """
    解压 ZIP 文件，递归提取其中支持的文档文本。
    返回所有提取到的文本片段列表。
    """
    texts = []
    with zipfile.ZipFile(filepath, "r") as zf:
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
    """
    ext = Path(filepath).suffix.lower()

    if ext == ".zip":
        texts = load_zip(filepath, upload_dir or os.path.dirname(filepath))
        return texts[0] if texts else ""

    loader = SUPPORTED_EXTENSIONS.get(ext)
    if loader is None:
        raise ValueError(
            f"不支持的文件格式: {ext}。支持的格式: {list(SUPPORTED_EXTENSIONS.keys())}"
        )

    return loader(filepath)


def load_documents_from_paths(filepaths: list[str], upload_dir: str = "") -> list[dict]:
    """
    批量加载文档，返回列表 [{"filepath": str, "filename": str, "content": str}, ...]
    """
    results = []
    for fp in filepaths:
        if not os.path.exists(fp):
            logger.warning(f"文件不存在，跳过: {fp}")
            continue
        try:
            content = load_single_document(fp, upload_dir)
            if content.strip():
                results.append(
                    {
                        "filepath": fp,
                        "filename": Path(fp).name,
                        "content": content,
                    }
                )
            else:
                logger.warning(f"文档内容为空: {fp}")
        except Exception as e:
            logger.error(f"文档加载失败: {fp} - {e}")
    return results
