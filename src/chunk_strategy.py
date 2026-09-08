"""
chunk_strategy.py - 分块逻辑核心

设计原则:
- 不同格式、不同内容类型采用不同的分块策略
- 优先保持语义完整性，chunk_size 是约束条件而非唯一标准
- 所有策略最终返回 List[Chunk]，包含 text + metadata
"""

import re
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Callable

from config import DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class Chunk:
    """统一分块数据结构"""
    text: str
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "metadata": self.metadata}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Chunk":
        return cls(text=data["text"], metadata=data["metadata"])


# ==================== 通用工具函数 ====================

def split_by_size(text: str, chunk_size: int, overlap: int) -> List[str]:
    """
    按固定大小切分文本，带重叠
    作为兜底策略，尽量不在句子中间切断
    """
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        # 尽量在句子边界切分
        if end < len(text):
            # 向前找最近的句号、问号、感叹号或换行
            for i in range(end, start + chunk_size // 2, -1):
                if i < len(text) and text[i] in "。！？\n":
                    end = i + 1
                    break
        chunks.append(text[start:end].strip())
        start = end - overlap

    return [c for c in chunks if c]


def merge_small_chunks(chunks: List[Chunk], min_size: int = 100) -> List[Chunk]:
    """合并过小的 chunk，避免产生太多碎片"""
    if not chunks:
        return []

    merged = []
    buffer = chunks[0]

    for chunk in chunks[1:]:
        if len(buffer.text) < min_size:
            # 合并到 buffer
            buffer.text += "\n" + chunk.text
            # 保留第一个的 metadata，追加来源信息
            if "merged_from" not in buffer.metadata:
                buffer.metadata["merged_from"] = [buffer.metadata.get("chunk_index", 0)]
            buffer.metadata["merged_from"].append(chunk.metadata.get("chunk_index", 0))
        else:
            merged.append(buffer)
            buffer = chunk

    merged.append(buffer)
    return merged


# ==================== 各格式分块策略 ====================

def chunk_md(text: str, metadata: Dict[str, Any], chunk_size: int = DEFAULT_CHUNK_SIZE,
             overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """
    Markdown 分块策略：按标题层级递归切分
    - 先按 H1/H2/H3 切分，保留标题上下文
    - 若某章节过大，再用固定大小切分
    """
    logger.info(f"[MD] 分块: {metadata.get('source', 'unknown')}")

    # 按标题分割 (## 或 ### 开头)
    heading_pattern = re.compile(r'^(#{1,3}\s+.+)$', re.MULTILINE)
    parts = heading_pattern.split(text)

    chunks = []
    current_heading = ""

    for i, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue

        # 判断是否是标题
        if heading_pattern.match(part):
            current_heading = part
            continue

        # 组合标题 + 内容
        full_text = f"{current_heading}\n{part}" if current_heading else part

        # 若内容过大，再按大小切分
        if len(full_text) > chunk_size:
            sub_texts = split_by_size(full_text, chunk_size, overlap)
            for j, sub in enumerate(sub_texts):
                chunks.append(Chunk(
                    text=sub,
                    metadata={
                        **metadata,
                        "chunk_index": len(chunks),
                        "sub_index": j,
                        "strategy": "md_heading_recursive",
                        "heading": current_heading,
                    }
                ))
        else:
            chunks.append(Chunk(
                text=full_text,
                metadata={
                    **metadata,
                    "chunk_index": len(chunks),
                    "strategy": "md_heading",
                    "heading": current_heading,
                }
            ))

    return merge_small_chunks(chunks)


def chunk_txt(text: str, metadata: Dict[str, Any], chunk_size: int = DEFAULT_CHUNK_SIZE,
              overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """
    TXT 分块策略：按段落/句子递归切分
    - 先按空行（段落）分割
    - 段落过大再按句子/固定大小切分
    """
    logger.info(f"[TXT] 分块: {metadata.get('source', 'unknown')}")

    # 按空行分割段落
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks = []
    for para in paragraphs:
        if len(para) <= chunk_size:
            chunks.append(Chunk(
                text=para,
                metadata={
                    **metadata,
                    "chunk_index": len(chunks),
                    "strategy": "txt_paragraph",
                }
            ))
        else:
            # 段落过大，按句子或固定大小切分
            sub_texts = split_by_size(para, chunk_size, overlap)
            for j, sub in enumerate(sub_texts):
                chunks.append(Chunk(
                    text=sub,
                    metadata={
                        **metadata,
                        "chunk_index": len(chunks),
                        "sub_index": j,
                        "strategy": "txt_paragraph_recursive",
                    }
                ))

    return merge_small_chunks(chunks)


def chunk_docx(documents: List[Dict[str, Any]], chunk_size: int = DEFAULT_CHUNK_SIZE,
               overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """
    DOCX 分块策略：根据类型区分
    - paragraph 类型：按段落递归切分（同 TXT）
    - table 类型：整表作为一个 chunk（表格语义紧凑）
    """
    logger.info(f"[DOCX] 分块: {documents[0]['metadata'].get('source', 'unknown') if documents else 'unknown'}")

    chunks = []
    for doc in documents:
        text = doc["text"]
        meta = doc["metadata"]
        doc_type = meta.get("type", "paragraph")

        if doc_type == "table":
            # 表格整表作为一个 chunk，不进一步切分
            chunks.append(Chunk(
                text=text,
                metadata={
                    **meta,
                    "chunk_index": len(chunks),
                    "strategy": "docx_table_whole",
                }
            ))
        else:
            # 段落类型，按段落递归切分
            paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
            for para in paragraphs:
                if len(para) <= chunk_size:
                    chunks.append(Chunk(
                        text=para,
                        metadata={
                            **meta,
                            "chunk_index": len(chunks),
                            "strategy": "docx_paragraph",
                        }
                    ))
                else:
                    sub_texts = split_by_size(para, chunk_size, overlap)
                    for j, sub in enumerate(sub_texts):
                        chunks.append(Chunk(
                            text=sub,
                            metadata={
                                **meta,
                                "chunk_index": len(chunks),
                                "sub_index": j,
                                "strategy": "docx_paragraph_recursive",
                            }
                        ))

    return merge_small_chunks(chunks)


def chunk_xlsx(documents: List[Dict[str, Any]], chunk_size: int = DEFAULT_CHUNK_SIZE,
               overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """
    XLSX 分块策略：每行已是一个独立 Document，直接转换
    - 若单行过长（罕见），再按大小切分
    """
    logger.info(f"[XLSX] 分块: {documents[0]['metadata'].get('source', 'unknown') if documents else 'unknown'}")

    chunks = []
    for doc in documents:
        text = doc["text"]
        meta = doc["metadata"]

        if len(text) <= chunk_size:
            chunks.append(Chunk(
                text=text,
                metadata={
                    **meta,
                    "chunk_index": len(chunks),
                    "strategy": "xlsx_row",
                }
            ))
        else:
            # 单行过长，兜底切分
            sub_texts = split_by_size(text, chunk_size, DEFAULT_CHUNK_OVERLAP)
            for j, sub in enumerate(sub_texts):
                chunks.append(Chunk(
                    text=sub,
                    metadata={
                        **meta,
                        "chunk_index": len(chunks),
                        "sub_index": j,
                        "strategy": "xlsx_row_recursive",
                    }
                ))

    return chunks


def chunk_pdf(documents: List[Dict[str, Any]], chunk_size: int = DEFAULT_CHUNK_SIZE,
              overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """
    PDF 分块策略：按页切分，页面过大再递归细分
    - 保留页码信息，方便溯源
    """
    logger.info(f"[PDF] 分块: {documents[0]['metadata'].get('source', 'unknown') if documents else 'unknown'}")

    chunks = []
    for doc in documents:
        text = doc["text"]
        meta = doc["metadata"]
        page_num = meta.get("page_number", 0)

        if len(text) <= chunk_size:
            chunks.append(Chunk(
                text=text,
                metadata={
                    **meta,
                    "chunk_index": len(chunks),
                    "strategy": "pdf_page",
                }
            ))
        else:
            # 单页内容过多，按段落/句子切分
            sub_texts = split_by_size(text, chunk_size, overlap)
            for j, sub in enumerate(sub_texts):
                chunks.append(Chunk(
                    text=sub,
                    metadata={
                        **meta,
                        "chunk_index": len(chunks),
                        "sub_index": j,
                        "strategy": "pdf_page_recursive",
                        "page_number": page_num,
                    }
                ))

    return merge_small_chunks(chunks)


# ==================== 统一分块路由 ====================

FORMAT_CHUNK_STRATEGY: Dict[str, Callable] = {
    "md": chunk_md,
    "txt": chunk_txt,
    "docx": chunk_docx,
    "xlsx": chunk_xlsx,
    "pdf": chunk_pdf,
}


def chunk_document(documents: List[Dict[str, Any]], chunk_size: int = DEFAULT_CHUNK_SIZE,
                   overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """
    统一分块入口
    根据 metadata["format"] 路由到对应策略
    """
    if not documents:
        return []

    # 获取格式类型（假设同一批 documents 格式相同）
    fmt = documents[0]["metadata"].get("format", "txt")
    strategy = FORMAT_CHUNK_STRATEGY.get(fmt)

    if strategy is None:
        logger.warning(f"未找到分块策略: {fmt}，使用默认策略")
        # 兜底：所有文档按 txt 策略处理
        all_text = "\n".join(d["text"] for d in documents)
        return chunk_txt(all_text, documents[0]["metadata"], chunk_size, overlap)

    # 调用对应策略
    if fmt in ["md", "txt"]:
        # 这两种格式传入的是单个大文本
        all_text = "\n".join(d["text"] for d in documents)
        return strategy(all_text, documents[0]["metadata"], chunk_size, overlap)
    else:
        # docx, xlsx, pdf 传入的是 Document 列表
        return strategy(documents, chunk_size, overlap)


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


# ==================== 序列化工具 ====================

def chunks_to_json(chunks: List[Chunk]) -> str:
    import json
    return json.dumps([c.to_dict() for c in chunks], ensure_ascii=False, indent=2)


def chunks_from_json(json_str: str) -> List[Chunk]:
    import json
    data = json.loads(json_str)
    return [Chunk.from_dict(item) for item in data]


if __name__ == "__main__":
    # 本地测试：需要先运行 document_loader.py 加载文档
    from document_loader import load_all_documents, documents_to_json

    result = load_all_documents()
    docs = result["documents"]

    # 按文件分组（同 source 的文档归为一组）
    from collections import defaultdict

    grouped = defaultdict(list)
    for d in docs:
        grouped[d.metadata["source"]].append(d.to_dict())

    all_chunks = chunk_all(list(grouped.values()))

    print(f"\n总分块数: {len(all_chunks)}")
    for c in all_chunks[:5]:
        print(f"\n[{c.metadata.get('format')}] {c.metadata.get('strategy')} | "
              f"len={len(c.text)} | source={Path(c.metadata['source']).name}")
        print(c.text[:200] + "..." if len(c.text) > 200 else c.text)
