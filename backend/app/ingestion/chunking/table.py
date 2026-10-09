"""
table.py - 表格内容分块（Markdown 表格感知 + 行级语义化 + PDF 表格修复）

从 src/chunk_strategy.py 拆出的"表格类"策略，依赖 base.py（Chunk/split_by_size/merge_small_chunks）。
涵盖：
- Markdown 表格判定与表格感知分块（is_markdown_table / chunk_md_table_aware）
- 超大表按行切分并前置表头（split_markdown_table / prepend_header）
- 行级语义化整句（table_rows_to_sentences 及列名中文化辅助）
- xlsx 分块（chunk_xlsx）
- PDF 表格修复与分块（unwrap_wrapped_table_lines / chunk_pdf 等）

注：统一路由里 "md" 走的是 chunk_md_table_aware（表格感知版），故本模块同时服务 md/pdf/xlsx。
"""

import logging
import re
from typing import Any, Dict, List

from app.core.config import settings
from app.ingestion.chunking.base import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    Chunk,
    merge_small_chunks,
    split_by_size,
)

logger = logging.getLogger(__name__)


# ==================== Markdown 表格感知工具 ====================

_TABLE_HEADER_RE = re.compile(r'^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')


def is_markdown_table(text: str) -> bool:
    """
    判断文本块是否为 Markdown 表格。
    判据：前两行均含 |，且第二行是 |---|:---| 形式的分隔行。
    参考 GitHub Markdown 表格语法。
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


# ==================== XLSX 分块 ====================

def chunk_xlsx(documents: List[Dict[str, Any]], chunk_size: int = DEFAULT_CHUNK_SIZE,
               overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[Chunk]:
    """
    XLSX 分块策略（v7 补行级语义化，与 docx/md 表格策略对齐）
    - openpyxl 路径: 每个 sheet 是一张完整 Markdown 表格(type=sheet)
        ≤ chunk_size → 整表保留(xlsx_table_whole, is_table=True)
        > chunk_size → split_markdown_table 按行切分，每子块自带表头(xlsx_table_row_split)
        两种情况都会额外产出 table_rows_to_sentences 行级语义句块(table_row_sentence)
        （长表行数>MAX_ROW_SENTENCE_ROWS 自动跳过，避免 chunk 爆炸）
    - pandas 降级路径: 每行独立 Document("表头: ... 数据: ...")，保持原有按大小切分
    """
    logger.info(f"[XLSX] 分块: {documents[0]['metadata'].get('source', 'unknown') if documents else 'unknown'}")
    chunks = []
    table_counter = 0  # 为行组块/行级句块生成稳定的 table_id
    for doc in documents:
        text = doc["text"]
        meta = doc["metadata"]
        if is_markdown_table(text):
            # sheet 级 Markdown 表格：走表格感知切分
            table_counter += 1
            table_id = f"xtbl_{table_counter}"
            if len(text) > chunk_size:
                sub_tables = split_markdown_table(text, max_rows_per_chunk=15)
                for j, sub in enumerate(sub_tables):
                    chunks.append(Chunk(
                        text=sub,
                        metadata={**meta, "chunk_index": len(chunks), "sub_index": j,
                                  "strategy": "xlsx_table_row_split", "is_table": True,
                                  "table_id": table_id, "parent_id": table_id,
                                  "total_sub_chunks": len(sub_tables)}
                    ))
            else:
                chunks.append(Chunk(
                    text=text,
                    metadata={**meta, "chunk_index": len(chunks),
                              "strategy": "xlsx_table_whole", "is_table": True,
                              "table_id": table_id, "is_parent": True}
                ))
            # 行级语义化（additive）：逐行生成"列名 为 值"整句，与行组块并存；
            # 让"以任一列值查同行其它列"的 lookup 能被 hybrid+rerank 稳定命中
            for rs in table_rows_to_sentences(text):
                chunks.append(Chunk(
                    text=rs["text"],
                    metadata={**meta, "chunk_index": len(chunks), "sub_index": rs["row_index"],
                              "strategy": "table_row_sentence", "is_table": True,
                              "table_id": table_id, "parent_id": table_id,
                              "is_parent": False, "row_index": rs["row_index"]}
                ))
        elif len(text) <= chunk_size:
            chunks.append(Chunk(text=text, metadata={**meta, "chunk_index": len(chunks), "strategy": "xlsx_row"}))
        else:
            sub_texts = split_by_size(text, chunk_size, DEFAULT_CHUNK_OVERLAP)
            for j, sub in enumerate(sub_texts):
                chunks.append(Chunk(text=sub, metadata={**meta, "chunk_index": len(chunks), "sub_index": j,
                                                        "strategy": "xlsx_row_recursive"}))
    return chunks


# ==================== PDF 表格修复与分块 ====================

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
