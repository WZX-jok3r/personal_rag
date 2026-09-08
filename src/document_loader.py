"""
document_loader.py - 多格式文档加载解析核心
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any
import logging

# 各格式解析库
import pymupdf as fitz  # 新版推荐用法
import pandas as pd
from docx import Document as DocxDocument

from config import (
    KNOWLEDGE_BASE_DIR,
    SUPPORTED_EXTENSIONS,
    PDF_SUBDIR,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class Document:
    """统一文档块数据结构"""
    text: str
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "metadata": self.metadata}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Document":
        return cls(text=data["text"], metadata=data["metadata"])


def compute_file_hash(file_path: Path) -> str:
    """计算文件 MD5 hash，用于增量更新判断"""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


# ==================== 各格式解析器 ====================

def load_txt(file_path: Path) -> List[Document]:
    """加载 .txt 文件"""
    logger.info(f"[TXT] 解析: {file_path.name}")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        if not text.strip():
            logger.warning(f"[TXT] 文件为空: {file_path.name}")
            return []
        return [
            Document(
                text=text,
                metadata={
                    "source": str(file_path),
                    "format": "txt",
                    "type": "text",
                },
            )
        ]
    except Exception as e:
        logger.error(f"[TXT] 解析失败 {file_path.name}: {e}")
        return []


def load_md(file_path: Path) -> List[Document]:
    """加载 .md 文件，保留原始 markdown 文本（后续 chunk_strategy 按标题切分）"""
    logger.info(f"[MD] 解析: {file_path.name}")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        if not text.strip():
            logger.warning(f"[MD] 文件为空: {file_path.name}")
            return []
        return [
            Document(
                text=text,
                metadata={
                    "source": str(file_path),
                    "format": "md",
                    "type": "markdown",
                },
            )
        ]
    except Exception as e:
        logger.error(f"[MD] 解析失败 {file_path.name}: {e}")
        return []


def load_docx(file_path: Path) -> List[Document]:
    """
    加载 .docx 文件
    - 段落文本按段落拆分，保留段落结构
    - 表格单独提取，保留行列结构
    """
    logger.info(f"[DOCX] 解析: {file_path.name}")
    documents = []
    try:
        doc = DocxDocument(file_path)

        # 1. 提取段落文本（过滤空段落）
        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        if paragraphs:
            para_text = "\n".join(paragraphs)
            documents.append(
                Document(
                    text=para_text,
                    metadata={
                        "source": str(file_path),
                        "format": "docx",
                        "type": "paragraph",
                        "paragraph_count": len(paragraphs),
                    },
                )
            )

        # 2. 提取表格（每个表格作为一个独立 Document）
        for table_idx, table in enumerate(doc.tables):
            rows = []
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells)
                rows.append(row_text)
            if rows:
                table_text = "\n".join(rows)
                documents.append(
                    Document(
                        text=table_text,
                        metadata={
                            "source": str(file_path),
                            "format": "docx",
                            "type": "table",
                            "table_index": table_idx,
                            "row_count": len(rows),
                        },
                    )
                )

        # 3. 提取图片中的文字（如果有的话）
        # 注：python-docx 不直接支持 OCR，若需要可后续接入 paddleocr/easyocr
        # 这里先标记图片数量
        image_count = len(doc.inline_shapes)
        if image_count > 0:
            logger.info(f"[DOCX] 检测到 {image_count} 张图片，暂不支持 OCR 提取: {file_path.name}")

        if not documents:
            logger.warning(f"[DOCX] 未提取到有效内容: {file_path.name}")
        return documents

    except Exception as e:
        logger.error(f"[DOCX] 解析失败 {file_path.name}: {e}")
        return []


def load_xlsx(file_path: Path) -> List[Document]:
    """
    加载 .xlsx 文件
    - 每个 sheet 独立处理
    - 每行作为一个 Document，附带表头信息
    """
    logger.info(f"[XLSX] 解析: {file_path.name}")
    documents = []
    try:
        xl = pd.ExcelFile(file_path)
        for sheet_name in xl.sheet_names:
            df = pd.read_excel(file_path, sheet_name=sheet_name)
            if df.empty:
                logger.warning(f"[XLSX] Sheet 为空: {sheet_name} in {file_path.name}")
                continue

            # 获取表头
            headers = df.columns.tolist()
            header_str = " | ".join(str(h) for h in headers)

            # 每行作为一个 Document
            for idx, row in df.iterrows():
                row_values = " | ".join(str(v) if pd.notna(v) else "" for v in row.values)
                # 组合表头 + 行数据，方便语义理解
                text = f"表头: {header_str}\n数据: {row_values}"
                documents.append(
                    Document(
                        text=text,
                        metadata={
                            "source": str(file_path),
                            "format": "xlsx",
                            "type": "row",
                            "sheet_name": sheet_name,
                            "row_index": int(idx),
                            "headers": headers,
                        },
                    )
                )

        if not documents:
            logger.warning(f"[XLSX] 未提取到有效内容: {file_path.name}")
        return documents

    except Exception as e:
        logger.error(f"[XLSX] 解析失败 {file_path.name}: {e}")
        return []


def load_pdf(file_path: Path) -> List[Document]:
    """
    加载 .pdf 文件
    - 按页提取文本，每页作为一个 Document
    - 若页面文本过长，后续 chunk_strategy 会进一步切分
    """
    logger.info(f"[PDF] 解析: {file_path.name}")
    documents = []
    try:
        doc = fitz.open(file_path)
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            text = page.get_text().strip()
            if text:
                documents.append(
                    Document(
                        text=text,
                        metadata={
                            "source": str(file_path),
                            "format": "pdf",
                            "type": "page",
                            "page_number": page_num + 1,  # 从1开始
                            "total_pages": len(doc),
                        },
                    )
                )
        doc.close()

        if not documents:
            logger.warning(f"[PDF] 未提取到文本内容（可能是扫描件/图片PDF）: {file_path.name}")
        return documents

    except Exception as e:
        logger.error(f"[PDF] 解析失败 {file_path.name}: {e}")
        return []


# ==================== 统一加载入口 ====================

FORMAT_LOADER_MAP = {
    ".txt": load_txt,
    ".md": load_md,
    ".docx": load_docx,
    ".xlsx": load_xlsx,
    ".pdf": load_pdf,
}


def load_file(file_path: Path) -> List[Document]:
    """根据文件后缀调用对应解析器"""
    ext = file_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        logger.warning(f"不支持的文件格式: {ext} ({file_path.name})")
        return []
    loader = FORMAT_LOADER_MAP.get(ext)
    if loader is None:
        return []
    return loader(file_path)


def scan_knowledge_base() -> List[Path]:
    """
    扫描 knowledge_base 目录，收集所有支持的文件
    PDF 文件放在 pdf_examples/ 子目录中
    """
    files = []
    if not KNOWLEDGE_BASE_DIR.exists():
        logger.error(f"知识库目录不存在: {KNOWLEDGE_BASE_DIR}")
        return files

    # 扫描根目录（非 pdf 文件）
    for ext in SUPPORTED_EXTENSIONS:
        if ext == ".pdf":
            continue  # pdf 单独处理
        files.extend(KNOWLEDGE_BASE_DIR.glob(f"*{ext}"))

    # 扫描 pdf_examples/ 子目录
    pdf_dir = KNOWLEDGE_BASE_DIR / PDF_SUBDIR
    if pdf_dir.exists():
        files.extend(pdf_dir.glob("*.pdf"))
    else:
        logger.warning(f"PDF 子目录不存在: {pdf_dir}")

    return sorted(files)


def load_all_documents() -> Dict[str, Any]:
    """
    加载知识库中所有文档
    返回: {
        "documents": List[Document],
        "stats": {"total_files": int, "total_chunks": int, "by_format": dict}
    }
    """
    file_paths = scan_knowledge_base()
    all_docs: List[Document] = []
    stats = {"total_files": 0, "total_chunks": 0, "by_format": {}}

    for fp in file_paths:
        docs = load_file(fp)
        if docs:
            all_docs.extend(docs)
            stats["total_files"] += 1
            stats["total_chunks"] += len(docs)
            fmt = fp.suffix.lower().lstrip(".")
            stats["by_format"][fmt] = stats["by_format"].get(fmt, 0) + 1

    logger.info(f"知识库加载完成: {stats}")
    return {"documents": all_docs, "stats": stats}


# ==================== 序列化工具（用于 processed_cache） ====================

def documents_to_json(docs: List[Document]) -> str:
    """Document 列表序列化为 JSON 字符串"""
    return json.dumps([doc.to_dict() for doc in docs], ensure_ascii=False, indent=2)


def documents_from_json(json_str: str) -> List[Document]:
    """JSON 字符串反序列化为 Document 列表"""
    data = json.loads(json_str)
    return [Document.from_dict(item) for item in data]


if __name__ == "__main__":
    # 本地测试
    result = load_all_documents()
    print(f"\n加载统计: {result['stats']}")
    for doc in result["documents"][:3]:
        print(f"\n--- [{doc.metadata.get('format', 'unknown')}] {doc.metadata.get('type', 'unknown')} ---")
        print(doc.text[:300] + "..." if len(doc.text) > 300 else doc.text)
