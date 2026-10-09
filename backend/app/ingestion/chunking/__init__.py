"""
chunking - 分块包（按内容类型分层）

结构（依赖单向 base ← table ← text ← 本包，无循环）：
- base.py : Chunk / 定长切分 / 页码清理 / 小块合并 / 序列化（内容类型无关基元）
- table.py: Markdown 表格感知、行级语义化、xlsx、PDF 表格修复与分块
- text.py : txt / md / docx 文本类分块
- 本文件  : 统一分块路由 chunk_document / chunk_all，并对外re-export公共 API

对外主入口：chunk_document(documents) —— indexer 只依赖它 + Chunk + chunks_to_json。
"""

import logging
from typing import Any, Callable, Dict, List

from app.ingestion.chunking.base import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    Chunk,
    chunks_from_json,
    chunks_to_json,
    clean_chunks_page_markers,
    clean_page_number_markers,
    merge_small_chunks,
    split_by_size,
)
from app.ingestion.chunking.table import (
    chunk_md_table_aware,
    chunk_pdf,
    chunk_xlsx,
    is_markdown_table,
    split_markdown_table,
    table_rows_to_sentences,
    unwrap_wrapped_table_lines,
)
from app.ingestion.chunking.text import chunk_docx, chunk_md, chunk_txt

logger = logging.getLogger(__name__)

# ==================== 统一分块路由 ====================

FORMAT_CHUNK_STRATEGY: Dict[str, Callable] = {
    "md": chunk_md_table_aware,
    "txt": chunk_txt,
    "docx": chunk_docx,
    "xlsx": chunk_xlsx,
    "pdf": chunk_pdf,
}


def chunk_document(documents: List[Dict[str, Any]], chunk_size: int = DEFAULT_CHUNK_SIZE,
                   overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """统一分块入口"""
    if not documents:
        return []

    fmt = documents[0]["metadata"].get("format", "txt")

    # === 核心修改：如果是经过 Marker 等 PDF 解析转出的 Markdown 文本，强制走 md 策略 ===
    if fmt == "pdf" and documents[0]["metadata"].get("parser", "") == "marker":
        fmt = "md"

    strategy = FORMAT_CHUNK_STRATEGY.get(fmt)

    if strategy is None:
        logger.warning(f"未找到分块策略: {fmt}，使用默认策略")
        all_text = "\n".join(d["text"] for d in documents)
        return chunk_txt(all_text, documents[0]["metadata"], chunk_size, overlap)

    if fmt in ["md", "txt"]:
        all_text = "\n".join(d["text"] for d in documents)
        chunks = strategy(all_text, documents[0]["metadata"], chunk_size, overlap)
    else:
        chunks = strategy(documents, chunk_size, overlap)

    # 对所有chunk执行页码标记清理
    cleaned_chunks = clean_chunks_page_markers(chunks)

    # 记录清理统计
    if len(chunks) != len(cleaned_chunks):
        logger.debug(f"[PageCleaner] chunk数量变化: {len(chunks)} -> {len(cleaned_chunks)}")

    return cleaned_chunks


def chunk_all(documents_list: List[List[Dict[str, Any]]], chunk_size: int = DEFAULT_CHUNK_SIZE,
              overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """
    批量分块：按文件分组，每组调用 chunk_document
    """
    all_chunks = []
    for doc_group in documents_list:
        chunks = chunk_document(doc_group, chunk_size, overlap)
        all_chunks.extend(chunks)

    logger.info(f"分块完成: 共 {len(all_chunks)} 个 chunk")
    return all_chunks


__all__ = [
    # 基元
    "Chunk", "DEFAULT_CHUNK_SIZE", "DEFAULT_CHUNK_OVERLAP",
    "split_by_size", "merge_small_chunks",
    "clean_page_number_markers", "clean_chunks_page_markers",
    "chunks_to_json", "chunks_from_json",
    # 表格类
    "is_markdown_table", "split_markdown_table", "table_rows_to_sentences",
    "unwrap_wrapped_table_lines", "chunk_md_table_aware", "chunk_xlsx", "chunk_pdf",
    # 文本类
    "chunk_txt", "chunk_md", "chunk_docx",
    # 路由
    "FORMAT_CHUNK_STRATEGY", "chunk_document", "chunk_all",
]
