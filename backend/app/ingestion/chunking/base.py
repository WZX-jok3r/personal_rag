"""
base.py - 分块通用基元（内容类型无关）

从 src/chunk_strategy.py 拆出的"底座"层，被 table.py / text.py 复用：
- Chunk 数据结构与 to_dict/from_dict
- split_by_size 定长兜底切分
- 页码标记清理（clean_page_number_markers / clean_chunks_page_markers）
- 受保护块判定与小块合并（_is_protected_chunk / merge_small_chunks）
- 序列化（chunks_to_json / chunks_from_json）

本模块不依赖 text/table，避免循环导入。
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List

from app.core.config import settings

# 配置兼容层：沿用原 UPPER_CASE 常量名（作为函数默认参数在导入期求值）
DEFAULT_CHUNK_SIZE = settings.default_chunk_size
DEFAULT_CHUNK_OVERLAP = settings.default_chunk_overlap

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


def _is_protected_chunk(chunk: "Chunk") -> bool:
    """
    判断是否为受保护块：表格块(is_table)与图片块(is_image)。
    受保护块是独立的语义单元，既不吞并相邻块，也不被相邻块吞并，
    以避免表格行列结构 / 图片 OCR 文本在合并时被破坏或改写元数据。
    """
    md = chunk.metadata or {}
    return bool(md.get("is_table") or md.get("is_image"))


def merge_small_chunks(chunks: List[Chunk], min_size: int = 100) -> List[Chunk]:
    """
    合并过小的文本 chunk，避免产生太多碎片（v6 修复缺陷1）。

    保护规则（核心修复）:
    - 表格块(is_table)、图片块(is_image)为受保护块，永远独立保留：
        1) 不会作为 buffer 去吞并后续 chunk（避免把正文/其它表格粘进来）
        2) 不会被前文的文本 buffer 吞并（避免 strategy/is_table 等元数据被改写）
    - 仅当 buffer 与当前 chunk 均为普通文本块，且 buffer 文本长度不足 min_size 时才合并。
    """
    if not chunks:
        return []

    merged: List[Chunk] = []
    buffer = None  # 只缓存"普通文本块"，受保护块不入 buffer

    for chunk in chunks:
        # 受保护块（表格/图片）：先落盘已有文本 buffer，再独立保留当前块
        if _is_protected_chunk(chunk):
            if buffer is not None:
                merged.append(buffer)
                buffer = None
            merged.append(chunk)
            continue

        # 当前为普通文本块
        if buffer is None:
            buffer = chunk
            continue

        # buffer 必为普通文本块：过短则吞并当前块，否则落盘 buffer 并以后者续接
        if len(buffer.text) < min_size:
            buffer.text += "\n" + chunk.text
            if "merged_from" not in buffer.metadata:
                buffer.metadata["merged_from"] = [buffer.metadata.get("chunk_index", 0)]
            buffer.metadata["merged_from"].append(chunk.metadata.get("chunk_index", 0))
        else:
            merged.append(buffer)
            buffer = chunk

    if buffer is not None:
        merged.append(buffer)
    return merged


# ==================== 序列化工具 ====================

def chunks_to_json(chunks: List[Chunk]) -> str:
    return json.dumps([c.to_dict() for c in chunks], ensure_ascii=False, indent=2)


def chunks_from_json(json_str: str) -> List[Chunk]:
    data = json.loads(json_str)
    return [Chunk.from_dict(item) for item in data]
