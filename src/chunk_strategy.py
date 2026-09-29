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
                # 行级语义化（additive）：逐行生成"ID 为 X 的员工，姓名为 …"整句，
                # 让"以任一列值查同行信息"能被 hybrid+rerank 稳定命中；原行组块保留以支撑聚合类问题
                for rs in table_rows_to_sentences(para):
                    chunks.append(Chunk(
                        text=rs["text"],
                        metadata={
                            **metadata,
                            "chunk_index": len(chunks),
                            "sub_index": rs["row_index"],
                            "strategy": "table_row_sentence",
                            "heading": current_heading,
                            "is_table": True,
                            "table_id": table_id,
                            "parent_id": table_id,
                            "is_parent": False,
                            "row_index": rs["row_index"],
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


# ==================== 表格行级语义化 ====================

# 常见列名 → 中文表达（键为小写去空白；未命中保留原文），用于行级句的列名中文化
COLUMN_CN_MAP = {
    "id": "ID", "no": "编号", "no.": "编号", "number": "编号", "code": "编号",
    "employee id": "编号", "emp id": "编号", "staff id": "编号",
    "name": "姓名", "full name": "姓名", "employee name": "姓名",
    "first name": "名", "last name": "姓",
    "email": "邮箱", "e-mail": "邮箱",
    "city": "所在城市", "town": "所在城市",
    "department": "部门", "dept": "部门", "team": "团队",
    "salary": "薪酬", "wage": "薪酬", "position": "职位", "title": "职位",
    "job title": "职位", "role": "职位",
    "company": "公司", "address": "地址", "phone": "电话", "mobile": "手机号",
    "date": "日期", "hire date": "入职日期", "start date": "开始日期",
    "location": "所在城市",
    "revenue": "收入", "price": "价格", "amount": "金额", "cost": "成本",
    "status": "状态", "plan": "订阅计划", "product": "产品", "model": "型号",
    "category": "类别", "feature": "功能", "members": "人数", "budget": "预算",
    "stock": "库存", "rating": "评分", "description": "描述",
    "age": "年龄", "gender": "性别", "country": "国家", "region": "地区",
}

# 行级语义化的最大表行数：更大的表（如 10000 行明细表）跳过，避免 chunk 数量爆炸与检索稀释
MAX_ROW_SENTENCE_ROWS = 200


def _humanize_column(col: str) -> str:
    """列名中文化：命中词典（忽略大小写与多余空白）则映射；带括号单位（如 Budget ($)）去括号后再匹配；否则保留原文"""
    key = " ".join(col.strip().split()).lower()
    if key in COLUMN_CN_MAP:
        return COLUMN_CN_MAP[key]
    base = re.sub(r"[\(（].*?[\)）]", "", key).strip()
    if base and base in COLUMN_CN_MAP:
        return COLUMN_CN_MAP[base]
    return col.strip()


def _guess_entity_word(headers: List[str]) -> str:
    """推断行的主体词：姓名+编号列特征 → 员工；产品/型号特征 → 产品；否则 记录"""
    joined = " ".join(h.strip().lower() for h in headers)
    has_name = ("name" in joined) or ("姓名" in joined)
    has_id = ("id" in joined) or ("编号" in joined)
    if has_name and has_id:
        return "员工"
    if ("产品" in joined) or ("product" in joined) or ("型号" in joined) or ("model" in joined):
        return "产品"
    return "记录"


def _find_key_col(headers: List[str]) -> int:
    """定位主键列：列名含 id/编号/no 的第一列；未命中用第 0 列"""
    for i, h in enumerate(headers):
        hl = h.strip().lower()
        if "id" in hl or "编号" in hl or hl in ("no", "no.", "number", "code"):
            return i
    return 0


def table_rows_to_sentences(table_text: str) -> List[Dict[str, Any]]:
    """
    将 Markdown 表格逐行转成"行级语义化整句"，用于"以任一列值查同行信息"（ID/姓名/城市互查）。
    句式（rerank 实测相关度 0.99+，显著优于原始表格行文本，解决纯数字锚点被同构大表淹没的问题）：
        "ID 为 37 的员工，姓名为 Tara Kuhlman，邮箱为 ...，所在城市为 South Evelynchester。"
    返回 [{"text": 语句, "row_index": 行序}]；非标准 Markdown 表格或行数 > MAX_ROW_SENTENCE_ROWS 时返回 []。
    """
    if not is_markdown_table(table_text):
        return []
    lines = [ln for ln in table_text.strip().split("\n") if ln.strip()]
    if len(lines) < 3:
        return []
    headers = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    data_lines = lines[2:]
    if len(data_lines) > MAX_ROW_SENTENCE_ROWS:
        return []
    key_col = _find_key_col(headers)
    entity = _guess_entity_word(headers)
    sentences: List[Dict[str, Any]] = []
    for i, ln in enumerate(data_lines):
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        if not any(cells):
            continue
        key_val = cells[key_col] if key_col < len(cells) else ""
        pairs = [
            f"{_humanize_column(h)}为 {v}"
            for ci, (h, v) in enumerate(zip(headers, cells))
            if ci != key_col and v
        ]
        if key_val:
            text = f"{_humanize_column(headers[key_col])} 为 {key_val} 的{entity}"
            text += ("，" + "，".join(pairs) + "。") if pairs else "。"
        else:
            text = "表格行：" + "，".join(
                f"{_humanize_column(h)}为 {v}" for h, v in zip(headers, cells) if v
            ) + "。"
        sentences.append({"text": text, "row_index": i})
    return sentences


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


def chunk_xlsx(documents: List[Dict[str, Any]], chunk_size: int = DEFAULT_CHUNK_SIZE,
               overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """
    XLSX 分块策略（v6 修复缺陷3：sheet 级 Markdown 大表走表格感知切分）
    - openpyxl 路径: 每个 sheet 是一张完整 Markdown 表格(type=sheet)
        ≤ chunk_size → 整表保留(xlsx_table_whole, is_table=True)
        > chunk_size → split_markdown_table 按行切分，每子块自带表头(xlsx_table_row_split)
        （与 docx/md 表格策略对齐，替代原先破坏行列结构的字符级切分）
    - pandas 降级路径: 每行独立 Document("表头: ... 数据: ...")，保持原有按大小切分
    """
    logger.info(f"[XLSX] 分块: {documents[0]['metadata'].get('source', 'unknown') if documents else 'unknown'}")
    chunks = []
    for doc in documents:
        text = doc["text"]
        meta = doc["metadata"]
        if is_markdown_table(text):
            # sheet 级 Markdown 表格：走表格感知切分
            if len(text) > chunk_size:
                sub_tables = split_markdown_table(text, max_rows_per_chunk=15)
                for j, sub in enumerate(sub_tables):
                    chunks.append(Chunk(
                        text=sub,
                        metadata={**meta, "chunk_index": len(chunks), "sub_index": j,
                                  "strategy": "xlsx_table_row_split", "is_table": True,
                                  "total_sub_chunks": len(sub_tables)}
                    ))
            else:
                chunks.append(Chunk(
                    text=text,
                    metadata={**meta, "chunk_index": len(chunks),
                              "strategy": "xlsx_table_whole", "is_table": True}
                ))
        elif len(text) <= chunk_size:
            chunks.append(Chunk(text=text, metadata={**meta, "chunk_index": len(chunks), "strategy": "xlsx_row"}))
        else:
            sub_texts = split_by_size(text, chunk_size, DEFAULT_CHUNK_OVERLAP)
            for j, sub in enumerate(sub_texts):
                chunks.append(Chunk(text=sub, metadata={**meta, "chunk_index": len(chunks), "sub_index": j,
                                                        "strategy": "xlsx_row_recursive"}))
    return chunks


# 页首标记行（loader 注入的页码标签/注记），路由表格检测前需剥离
_PAGE_META_LINE_RE = re.compile(
    r'^(【第.*页.*】|文档标注页码.*|文件物理页码.*|Page\s+\d+\s+of\s+\d+.*)$'
)


def _strip_leading_page_markers(text: str) -> str:
    """剥离页面文本开头的页码标签/注记行（含可能的前导空行）"""
    lines = text.split("\n")
    idx = 0
    while idx < len(lines) and (not lines[idx].strip() or _PAGE_META_LINE_RE.match(lines[idx].strip())):
        idx += 1
    return "\n".join(lines[idx:])


def unwrap_wrapped_table_lines(text: str) -> str:
    """
    修复"宽表格单元格被 PDF 提取器按视觉换行折断"导致的 Markdown 表格失真。

    背景：pdfplumber / pymupdf 提取宽表（如 A3 绩效表）时，会把一个逻辑行拆成
    多个物理行——表头 "Revenue Generated" 被折成 "... | Revenue" + "Generated | ..."，
    数据行 "Quality Assurance" 同理。于是表格不再满足"一行一记录、行首行尾都含 |"
    的 Markdown 约定，is_markdown_table（要求第 2 行是 |---| 分隔行）判定失败，
    整表被 split_by_size 按字符切碎，表头与数据行分离（如 Design 部门行丢表头）。

    规则（保守，只处理表格行，绝不触碰正文）:
    - 仅当物理行 lstrip 后以 `|` 开头、却未以 `|` 结尾时，视为被折断的表格行起点；
    - 向后拼接后续物理行（以单空格连接）直到累计缓冲以 `|` 收尾（该行结构闭合）；
    - 遇到空行则放弃拼接（表格到此为止），原样保留当前行，避免吞并正文段落。
    已是完整表格行（首尾都有 `|`）及非表格行均原样输出。
    """
    if "|" not in text:
        return text
    lines = text.split("\n")
    out: List[str] = []
    i, n = 0, len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.rstrip()
        # 折行起点：以 | 开头却没以 | 结尾
        if stripped.lstrip().startswith("|") and not stripped.endswith("|"):
            buf = stripped
            j = i + 1
            closed = False
            while j < n:
                nxt = lines[j].strip()
                if not nxt:  # 空行：表格在此中断，放弃拼接
                    break
                buf = buf + " " + nxt
                j += 1
                if buf.endswith("|"):  # 列结构闭合
                    closed = True
                    break
            if closed:
                out.append(buf)
                i = j
                continue
            # 未闭合：不改动，原样输出当前行后逐行推进
            out.append(raw)
            i += 1
        else:
            out.append(raw)
            i += 1
    return "\n".join(out)


def _body_contains_markdown_table(body: str) -> bool:
    """按空行分块检测页面正文中是否存在 Markdown 表格（判据严格，避免误触发）"""
    return any(is_markdown_table(block) for block in re.split(r'\n\s*\n', body))


def chunk_pdf(documents: List[Dict[str, Any]], chunk_size: int = DEFAULT_CHUNK_SIZE,
              overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """
    PDF 分块策略（v6 修复缺陷2：逐页路由，页级元数据不再丢失）
    1. 逐页剥离页首标记行后检测 Markdown 表格
    2. 含表页 → 仅该页正文走 md 表格感知策略，metadata 用该页自身（页码归属正确）
    3. 无表页/表格策略异常 → 该页走原有分页分块逻辑
    4. 小块合并仅在页内进行，不再跨页吞并（跨页合并会破坏页码归属）
    """
    if not documents:
        return []

    source = documents[0]["metadata"].get("source", "unknown")
    logger.info(f"[PDF-Optimized] 分块: {source}")

    chunks: List[Chunk] = []
    for doc in documents:
        text = doc["text"]
        meta = doc["metadata"]
        page_num = meta.get("page_number", 0)
        page_label = meta.get("page_label", f"第{page_num}页")

        # 剥离页首标记后的正文（表格检测/表格切分都基于纯正文）
        body = _strip_leading_page_markers(text)
        # 修复宽表被换行折断的行：把多物理行拼回标准 Markdown 表行，
        # 使含表页能被 _body_contains_markdown_table 识别，进而复用
        # split_markdown_table（表头随行）+ table_rows_to_sentences（行级语义）
        body = unwrap_wrapped_table_lines(body)

        # === 含表页：该页单独走表格感知策略（修复前是整个PDF共用首页metadata） ===
        if _body_contains_markdown_table(body):
            try:
                page_chunks = chunk_md_table_aware(body, meta, chunk_size, overlap)
                for c in page_chunks:
                    c.metadata["page_label"] = page_label
                    c.metadata["chunk_index"] = len(chunks)
                    chunks.append(c)
                logger.info(f"[PDF-Optimized] 第{page_num}页检测到Markdown表格，使用页级md表格感知策略")
                continue
            except Exception as e:
                logger.warning(f"[PDF-Optimized] 第{page_num}页表格感知策略失败，回退普通分页策略: {e}")

        # === 普通页分块逻辑（与原实现一致） ===
        page_text = body
        if not page_text.strip().startswith("【第"):
            page_text = f"{page_label}\n{page_text}"

        if len(page_text) <= chunk_size:
            chunks.append(Chunk(text=page_text, metadata={
                **meta,
                "chunk_index": len(chunks),
                "strategy": "pdf_page_optimized",
                "page_label": page_label
            }))
        else:
            sub_texts = split_by_size(page_text, chunk_size, overlap)
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

    # 逐页合并小块（chunk_md_table_aware 内部已合并过的表格页会被幂等重复合并，无副作用）
    final_chunks: List[Chunk] = []
    # 页边界由 chunk 的 page_number 元数据识别：仅合并同页内的相邻小块
    prev_page = object()
    buffered: List[Chunk] = []
    for c in chunks:
        pg = c.metadata.get("page_number")
        if pg != prev_page:
            for m in merge_small_chunks(buffered):
                m.metadata["chunk_index"] = len(final_chunks)
                final_chunks.append(m)
            buffered = []
            prev_page = pg
        buffered.append(c)
    for m in merge_small_chunks(buffered):
        m.metadata["chunk_index"] = len(final_chunks)
        final_chunks.append(m)

    return final_chunks


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
