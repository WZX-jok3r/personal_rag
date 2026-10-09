"""
text.py - 文本内容分块（txt / md / docx）

从 src/chunk_strategy.py 拆出的"文本类"策略，依赖 base.py（基元）与 table.py（表格感知工具）。
- chunk_txt:  段落/句子递归切分
- chunk_md:   标题层级 + 表格感知（legacy；统一路由 "md" 实际走 table.chunk_md_table_aware）
- chunk_docx: 段落 / 图片(OCR整块) / 表格(复用 md 表格切分 + 行级语义化)
"""

import logging
import re
from typing import Any, Dict, List

from app.ingestion.chunking.base import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    Chunk,
    merge_small_chunks,
    split_by_size,
)
from app.ingestion.chunking.table import (
    is_markdown_table,
    split_markdown_table,
    table_rows_to_sentences,
)

logger = logging.getLogger(__name__)


def chunk_md(text: str, metadata: Dict[str, Any], chunk_size: int = DEFAULT_CHUNK_SIZE,
             overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """
    Markdown 分块策略（表格感知版）：
    - 按标题层级递归切分
    - 检测到表格时，若超过 chunk_size，则按行切分并在每个子块前补全表头
    """
    logger.info(f"[MD] 分块: {metadata.get('source', 'unknown')}")

    heading_pattern = re.compile(r'^(#{1,3}\s+.+)$', re.MULTILINE)
    parts = heading_pattern.split(text)

    chunks = []
    current_heading = ""

    for i, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue

        if heading_pattern.match(part):
            current_heading = part
            continue

        # 将当前部分按空行分割为更小的段落块，防止混合内容干扰
        paragraphs = re.split(r'\n\s*\n', part)

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            full_text = f"{current_heading}\n{para}" if current_heading else para

            # === 核心：表格感知处理 ===
            if is_markdown_table(para):
                if len(full_text) > chunk_size:
                    # 表格过大，按行切分并补全表头
                    # 【缺陷4修复】原误写为 max_rows=10，形参名应为 max_rows_per_chunk
                    sub_tables = split_markdown_table(para, max_rows_per_chunk=10)
                    for j, sub in enumerate(sub_tables):
                        # 组装：标题 + 表头 + 数据行
                        sub_text = f"{current_heading}\n{sub}" if current_heading else sub
                        chunks.append(Chunk(
                            text=sub_text,
                            metadata={
                                **metadata,
                                "chunk_index": len(chunks),
                                "sub_index": j,
                                "strategy": "md_table_row_split",  # 标记策略
                                "heading": current_heading,
                                "is_table": True
                            }
                        ))
                else:
                    # 表格不大，整块保留
                    chunks.append(Chunk(
                        text=full_text,
                        metadata={
                            **metadata,
                            "chunk_index": len(chunks),
                            "strategy": "md_table_whole",
                            "heading": current_heading,
                            "is_table": True
                        }
                    ))
            # === 常规文本处理 ===
            else:
                if len(full_text) > chunk_size:
                    sub_texts = split_by_size(full_text, chunk_size, overlap)
                    for j, sub in enumerate(sub_texts):
                        chunks.append(Chunk(
                            text=sub,
                            metadata={
                                **metadata,
                                "chunk_index": len(chunks),
                                "sub_index": j,
                                "strategy": "md_text_recursive",
                                "heading": current_heading
                            }
                        ))
                else:
                    chunks.append(Chunk(
                        text=full_text,
                        metadata={
                            **metadata,
                            "chunk_index": len(chunks),
                            "strategy": "md_text",
                            "heading": current_heading
                        }
                    ))

    return merge_small_chunks(chunks)


def chunk_txt(text: str, metadata: Dict[str, Any], chunk_size: int = DEFAULT_CHUNK_SIZE,
              overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """TXT 分块策略：按段落/句子递归切分"""
    logger.info(f"[TXT] 分块: {metadata.get('source', 'unknown')}")
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks = []
    for para in paragraphs:
        if len(para) <= chunk_size:
            chunks.append(
                Chunk(text=para, metadata={**metadata, "chunk_index": len(chunks), "strategy": "txt_paragraph"}))
        else:
            sub_texts = split_by_size(para, chunk_size, overlap)
            for j, sub in enumerate(sub_texts):
                chunks.append(Chunk(text=sub, metadata={**metadata, "chunk_index": len(chunks), "sub_index": j,
                                                        "strategy": "txt_paragraph_recursive"}))
    return merge_small_chunks(chunks)


def chunk_docx(documents: List[Dict[str, Any]], chunk_size: int = DEFAULT_CHUNK_SIZE,
               overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """
    DOCX 分块策略（v3 优化）：
    - 表格：标准 Markdown 表格 ≤ chunk_size → 整表保留；
            超长表格 → 复用 split_markdown_table 按行切分，每个子块自带表头
             （与 PDF md 表格策略对齐，保证行列语义完整）
    - 段落：按段落保留，超长段递归切分
    """
    logger.info(f"[DOCX] 分块: {documents[0]['metadata'].get('source', 'unknown') if documents else 'unknown'}")
    chunks = []
    table_counter = 0  # 表格计数：为行组块/行级句块生成稳定的 table_id
    for doc in documents:
        text = doc["text"]
        meta = doc["metadata"]
        doc_type = meta.get("type", "paragraph")
        if doc_type == "image":
            # 图片独立 chunk：OCR 文本整体保留，不按段落切分
            # （避免 OCR 文本被误切成半句话，且图片内容应作为单一语义单元）
            chunks.append(Chunk(
                text=text,
                metadata={
                    **meta,
                    "chunk_index": len(chunks),
                    "strategy": "docx_image_whole",
                    "is_image": True,
                    "has_ocr": meta.get("has_ocr", False),
                }
            ))
        elif doc_type == "table":
            table_counter += 1
            table_id = f"dtbl_{table_counter}"
            if is_markdown_table(text) and len(text) > chunk_size:
                # 超长表格：按行切分，每子块前补表头（复用 md 表格切分逻辑）
                sub_tables = split_markdown_table(text, max_rows_per_chunk=15)
                for j, sub in enumerate(sub_tables):
                    chunks.append(Chunk(
                        text=sub,
                        metadata={
                            **meta,
                            "chunk_index": len(chunks),
                            "sub_index": j,
                            "strategy": "docx_table_row_split",
                            "is_table": True,
                            "table_id": table_id,
                            "parent_id": table_id,
                            "total_sub_chunks": len(sub_tables),
                        }
                    ))
            else:
                # 小表格或非标准表格：整表保留
                chunks.append(
                    Chunk(text=text, metadata={**meta, "chunk_index": len(chunks), "strategy": "docx_table_whole",
                                               "is_table": True, "table_id": table_id, "is_parent": True}))
            # 行级语义化（additive）：逐行生成整句，供"以任一列值查同行信息"
            for rs in table_rows_to_sentences(text):
                chunks.append(Chunk(
                    text=rs["text"],
                    metadata={
                        **meta,
                        "chunk_index": len(chunks),
                        "sub_index": rs["row_index"],
                        "strategy": "table_row_sentence",
                        "is_table": True,
                        "table_id": table_id,
                        "parent_id": table_id,
                        "is_parent": False,
                        "row_index": rs["row_index"],
                    }
                ))
        else:
            paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
            for para in paragraphs:
                if len(para) <= chunk_size:
                    chunks.append(
                        Chunk(text=para, metadata={**meta, "chunk_index": len(chunks), "strategy": "docx_paragraph"}))
                else:
                    sub_texts = split_by_size(para, chunk_size, overlap)
                    for j, sub in enumerate(sub_texts):
                        chunks.append(Chunk(text=sub, metadata={**meta, "chunk_index": len(chunks), "sub_index": j,
                                                                "strategy": "docx_paragraph_recursive"}))
    return merge_small_chunks(chunks)
