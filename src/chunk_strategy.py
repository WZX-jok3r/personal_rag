"""
chunk_strategybak.py - 分块逻辑核心

设计原则:
- 不同格式、不同内容类型采用不同的分块策略
- 优先保持语义完整性，chunk_size 是约束条件而非唯一标准
- 引入表格感知：对于 Markdown 表格，提取表头并在切分时预置到每个 chunk
"""

import re
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Callable

# 假设 config 中有默认配置
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
    """按固定大小切分文本，带重叠，兜底策略"""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end < len(text):
            for i in range(end, start + chunk_size // 2, -1):
                if i < len(text) and text[i] in "。！？\n":
                    end = i + 1
                    break
        chunks.append(text[start:end].strip())
        start = end - overlap
    return [c for c in chunks if c]


def clean_page_number_markers(text: str) -> str:
    """
    清理PDF chunk中的页码标记和元数据说明行
    
    清理内容：
    1. 页码标签行：如 "【第39页(逻辑)/50页】"
    2. 页码说明行：如 "文档标注页码:39, 文件物理页码:48"
    3. 脚注页码：如 "Page 39 of 50 | ExampleFiles.org"
    
    保留：
    - 正文内容
    - 表格内容
    - 标题和章节信息
    """
    import re
    
    lines = text.split('\n')
    cleaned_lines = []
    
    # 定义需要清理的模式
    page_patterns = [
        r'^【第.*页.*】$',  # 页码标签行
        r'^文档标注页码[:：].*$',  # 页码说明
        r'^文件物理页码[:：].*$',  # 物理页码说明
        r'^Page\s+\d+\s+of\s+\d+.*$',  # Page 39 of 50
        r'第\d+页\([逻物]\)',  # 第39页(逻), 第48页(物)
    ]
    
    for line in lines:
        line_stripped = line.strip()
        should_keep = True
        
        # 检查是否匹配清理模式
        for pattern in page_patterns:
            if re.match(pattern, line_stripped):
                should_keep = False
                break
        
        # 特殊处理：页码说明行可能包含有用上下文
        if "文档标注页码" in line_stripped or "文件物理页码" in line_stripped:
            # 如果这一行包含实际内容（不只是元数据），保留部分信息
            if "，" in line_stripped or "," in line_stripped:
                # 可能包含有用信息，我们只保留简洁的页码信息
                page_info_match = re.search(r'文档标注页码[:：](\d+)', line_stripped)
                if page_info_match:
                    page_num = page_info_match.group(1)
                    cleaned_lines.append(f"文档页码：{page_num}")
                continue
        
        # 保留非空行且不匹配清理模式的
        if should_keep and line_stripped:
            cleaned_lines.append(line)
    
    # 清理开头和结尾的空白行
    while cleaned_lines and not cleaned_lines[0].strip():
        cleaned_lines.pop(0)
    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()
    
    return '\n'.join(cleaned_lines)


def clean_chunks_page_markers(chunks: List[Chunk]) -> List[Chunk]:
    """
    清理一批chunk中的页码标记
    
    根据metadata确定清理策略：
    - 对于表格chunk：完全保留（可能包含有用的行列信息）
    - 对于带逻辑页码的chunk：可以去除页码标记  
    - 对于标题/正文chunk：选择性清理
    """
    cleaned_chunks = []
    
    for chunk in chunks:
        metadata = chunk.metadata
        text = chunk.text
        
        # 检查chunk类型决定清理策略
        is_table = metadata.get("is_table", False)
        has_logical_page = metadata.get("has_logical_page", False)
        format_type = metadata.get("format", "unknown")
        
        if is_table:
            # 表格chunk：保留完整内容（可能包含页码标记作为上下文）
            cleaned_text = text
        elif format_type == "pdf" and has_logical_page:
            # PDF带逻辑页码的chunk：清理页码标记但保留基本页码信息
            cleaned_text = clean_page_number_markers(text)
            
            # 如果清理后为空，保留第一行（通常有有价值的内容）
            if not cleaned_text.strip() and text:
                first_line = text.split('\n')[0]
                if first_line.strip() and not re.match(r'^【第.*页.*】$', first_line.strip()):
                    cleaned_text = first_line
        else:
            # 其他chunk：选择性清理
            cleaned_text = clean_page_number_markers(text)
        
        # 创建清理后的chunk
        cleaned_chunks.append(Chunk(
            text=cleaned_text,
            metadata={**metadata, "page_markers_cleaned": True}
        ))
    
    return cleaned_chunks


def merge_small_chunks(chunks: List[Chunk], min_size: int = 100) -> List[Chunk]:
    """合并过小的 chunk，避免产生太多碎片"""
    if not chunks:
        return []
    merged = []
    buffer = chunks[0]
    for chunk in chunks[1:]:
        if len(buffer.text) < min_size:
            buffer.text += "\n" + chunk.text
            if "merged_from" not in buffer.metadata:
                buffer.metadata["merged_from"] = [buffer.metadata.get("chunk_index", 0)]
            buffer.metadata["merged_from"].append(chunk.metadata.get("chunk_index", 0))
        else:
            merged.append(buffer)
            buffer = chunk
    merged.append(buffer)
    return merged


# ==================== 新增：Markdown 表格处理工具 ====================

# ==================== Markdown 表格感知工具 ====================

_TABLE_HEADER_RE = re.compile(r'^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')


def is_markdown_table(text: str) -> bool:
    """
    判断文本块是否为 Markdown 表格。
    判据：前两行均含 |，且第二行是 |---|:---| 形式的分隔行。
    参考 GitHub Markdown 表格语法<span data-allow-html class='source-item source-aggregated' data-group-key='source-group-1' data-url='https://formatarc&#46;com' data-id='turn1search4'><span data-allow-html class='source-item-num' data-group-key='source-group-1' data-id='turn1search4' data-url='https://formatarc&#46;com'><span class='source-item-num-name' data-allow-html>formatarc.com</span><span data-allow-html class='source-item-num-count'>+1</span></span></span>
    """
    lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
    if len(lines) < 2:
        return False
    if "|" not in lines[0]:
        return False
    # 第二行必须全是 -、:、|、空格
    if not _TABLE_HEADER_RE.match(lines[1]):
        return False
    return True

def chunk_md_table_aware(text: str, metadata: Dict[str, Any],
                         chunk_size: int = DEFAULT_CHUNK_SIZE,
                         overlap: int = DEFAULT_CHUNK_OVERLAP,
                         max_rows_per_chunk: int = 15) -> List[Chunk]:
    """
    表格感知 Markdown 分块策略：
    - 先按 H1/H2/H3 标题层级切分
    - 段落级再按空行细分
    - 遇到 Markdown 表格：整表 ≤ chunk_size → 作为父块保留
                         整表 > chunk_size → split_markdown_table 按行切分，
                         每子块前补表头，子块通过 parent_id 关联到 table_id
    """
    logger.info(f"[MD-TableAware] 分块: {metadata.get('source', 'unknown')}")

    heading_pattern = re.compile(r'^(#{1,3}\s+.+)$', re.MULTILINE)
    parts = heading_pattern.split(text)

    chunks: List[Chunk] = []
    current_heading = ""
    table_counter = 0

    for part in parts:
        part = part.strip()
        if not part:
            continue
        if heading_pattern.match(part):
            current_heading = part
            continue

        # 按空行切分段落，避免表格与正文粘连
        paragraphs = re.split(r'\n\s*\n', part)
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            full_text = f"{current_heading}\n{para}" if current_heading else para

            # ===== 表格感知分支 =====
            if is_markdown_table(para):
                table_counter += 1
                table_id = f"tbl_{table_counter}"

                if len(para) <= chunk_size:
                    # 整表保留 → 父块
                    chunks.append(Chunk(
                        text=full_text,
                        metadata={
                            **metadata,
                            "chunk_index": len(chunks),
                            "strategy": "md_table_whole",
                            "heading": current_heading,
                            "is_table": True,
                            "table_id": table_id,
                            "parent_id": None,
                            "is_parent": True,
                        }
                    ))
                else:
                    # 超大表 → 按行切分，每子块前补表头
                    sub_tables = split_markdown_table(para, max_rows_per_chunk)
                    for j, sub in enumerate(sub_tables):
                        sub_text = (f"{current_heading}\n{sub}"
                                    if current_heading else sub)
                        chunks.append(Chunk(
                            text=sub_text,
                            metadata={
                                **metadata,
                                "chunk_index": len(chunks),
                                "sub_index": j,
                                "strategy": "md_table_row_split",
                                "heading": current_heading,
                                "is_table": True,
                                "table_id": table_id,
                                "parent_id": table_id,
                                "is_parent": False,
                                "total_sub_chunks": len(sub_tables),
                            }
                        ))
            # ===== 常规文本分支 =====
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
                                "heading": current_heading,
                                "is_table": False,
                            }
                        ))
                else:
                    chunks.append(Chunk(
                        text=full_text,
                        metadata={
                            **metadata,
                            "chunk_index": len(chunks),
                            "strategy": "md_text",
                            "heading": current_heading,
                            "is_table": False,
                        }
                    ))

    return merge_small_chunks(chunks)


def prepend_header(header_block: str, body_text: str, caption: str = "") -> str:
    """
    将表头块前置到表格正文，组装成完整的子表格 markdown。
    可选 caption（表格标题/所在章节标题）一并前置，增强语义。
    """
    parts = []
    if caption:
        parts.append(caption.strip())
    parts.append(header_block.strip())
    parts.append(body_text.strip())
    return "\n".join(parts)


def split_markdown_table(table_text: str,
                         max_rows_per_chunk: int = 15) -> List[str]:
    """
    将超大 Markdown 表格按行分组切分，每个子块返回时已自带表头。
    - 前 2 行（表头行 + 分隔行）作为 header_block
    - 正文按 max_rows_per_chunk 分组
    - 每组用 prepend_header 重新拼成完整子表
    """
    lines = [ln for ln in table_text.strip().split("\n") if ln.strip()]
    if len(lines) <= 2:
        return [table_text]

    header_block = "\n".join(lines[:2])
    body_lines = lines[2:]

    chunks = []
    for i in range(0, len(body_lines), max_rows_per_chunk):
        row_group = "\n".join(body_lines[i:i + max_rows_per_chunk])
        chunks.append(prepend_header(header_block, row_group))
    return chunks


# ==================== 各格式分块策略 ====================

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
                    sub_tables = split_markdown_table(para, max_rows=10)
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
    """DOCX 分块策略：表格整表保留"""
    logger.info(f"[DOCX] 分块: {documents[0]['metadata'].get('source', 'unknown') if documents else 'unknown'}")
    chunks = []
    for doc in documents:
        text = doc["text"]
        meta = doc["metadata"]
        doc_type = meta.get("type", "paragraph")
        if doc_type == "table":
            chunks.append(
                Chunk(text=text, metadata={**meta, "chunk_index": len(chunks), "strategy": "docx_table_whole"}))
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


def chunk_xlsx(documents: List[Dict[str, Any]], chunk_size: int = DEFAULT_CHUNK_SIZE,
               overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """XLSX 分块策略：每行独立"""
    logger.info(f"[XLSX] 分块: {documents[0]['metadata'].get('source', 'unknown') if documents else 'unknown'}")
    chunks = []
    for doc in documents:
        text = doc["text"]
        meta = doc["metadata"]
        if len(text) <= chunk_size:
            chunks.append(Chunk(text=text, metadata={**meta, "chunk_index": len(chunks), "strategy": "xlsx_row"}))
        else:
            sub_texts = split_by_size(text, chunk_size, DEFAULT_CHUNK_OVERLAP)
            for j, sub in enumerate(sub_texts):
                chunks.append(Chunk(text=sub, metadata={**meta, "chunk_index": len(chunks), "sub_index": j,
                                                        "strategy": "xlsx_row_recursive"}))
    return chunks


def chunk_pdf(documents: List[Dict[str, Any]], chunk_size: int = DEFAULT_CHUNK_SIZE,
              overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """
    PDF 分块策略优化版：检测PDF提取的Markdown表格内容
    1. 如果包含Markdown表格结构，路由到md表格感知策略
    2. 否则使用原有分页分块逻辑
    """
    if not documents:
        return []
    
    source = documents[0]["metadata"].get("source", "unknown")
    logger.info(f"[PDF-Optimized] 分块: {source}")
    
    # 检测是否为包含表格的PDF（合并所有页面文本检查）
    all_text = "\n".join(doc["text"] for doc in documents)
    metadata = documents[0]["metadata"]
    
    # 检测Markdown表格特征
    has_markdown_table = is_markdown_table(all_text) or ("|" in all_text and "---" in all_text)
    
    if has_markdown_table:
        logger.info(f"[PDF-Optimized] 检测到Markdown表格内容: {source}，使用md表格感知策略")
        try:
            # 尝试使用md表格感知策略
            return chunk_md_table_aware(all_text, metadata, chunk_size, overlap)
        except Exception as e:
            logger.warning(f"[PDF-Optimized] md表格感知策略失败，回退到普通分页策略: {e}")
            # 回退到原逻辑
            pass
    
    # 普通PDF分页分块逻辑
    logger.info(f"[PDF-Optimized] 使用分页分块策略: {source}")
    chunks = []
    for doc in documents:
        text = doc["text"]
        meta = doc["metadata"]
        page_num = meta.get("page_number", 0)
        page_label = meta.get("page_label", f"第{page_num}页")
        
        # 保留页码信息的上下文
        if not text.strip().startswith("【第"):
            text = f"{page_label}\n{text}"
        
        if len(text) <= chunk_size:
            chunks.append(Chunk(text=text, metadata={
                **meta, 
                "chunk_index": len(chunks), 
                "strategy": "pdf_page_optimized",
                "page_label": page_label
            }))
        else:
            sub_texts = split_by_size(text, chunk_size, overlap)
            for j, sub in enumerate(sub_texts):
                # 确保子块也保持页码上下文
                if not sub.strip().startswith(page_label):
                    sub = f"{page_label}\n{sub}"
                chunks.append(Chunk(text=sub, metadata={
                    **meta, 
                    "chunk_index": len(chunks), 
                    "sub_index": j,
                    "strategy": "pdf_page_optimized_recursive",
                    "page_label": page_label
                }))
    
    # 合并小chunk前，确保合并时保留原始页面分组信息
    return merge_small_chunks(chunks)


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

# ... 以下序列化工具和 __main__ 部分保持不变 ...

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
