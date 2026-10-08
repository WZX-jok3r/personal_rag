"""schema_infer.py - 从表格原始行推断列类型与表头（纯函数，无 I/O，可单测）。

设计原则：
1. **纯函数**：输入 List[List[str]]，输出结构化 schema。不读文件、不连数据库。
   这样它可以被穷举单测，而 ETL 只负责 I/O 与调用它。
2. **全列判据，不抽样**：类型判定要求列内**所有**非空值都满足条件。
   抽样会引入不确定性 —— 同一份文件两次跑出不同 schema 是不可接受的。
3. **中文列名不硬转拼音**：中文表头保留在 `description` 里（进 SQL COMMENT 与
   LLM prompt），物理列名用稳定的 `col_N`。理由：拼音转换需要额外依赖、
   且多音字容易转错；而 LLM 拿到 `col_3 单价(元)` 完全够用（实测中文列名
   生成 SQL 无障碍）。若将来要"好看"的列名，走元数据表人工校正，不改这里。

与 RAG 通道的关系：**完全独立**。
本模块刻意不 import app.ingestion.loader —— loader 会连带拉入 pandas/pymupdf 等
重依赖，而 analytics 只需要几个纯字符串工具。为此本地重实现
`_parse_number` / `_is_sequence`（与 loader 同口径，见文末注释）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ==================== 常量 ====================

# 低基数阈值：distinct <= 该值的文本列，取值清单会内联进 COMMENT 与 prompt
# 理由：大小写/单复数写错是中文 NL2SQL 的头号错误来源，内联取值可根治
ENUM_MAX_DISTINCT = 50
# 枚举值最大长度（超长的"枚举"其实是自由文本，内联没意义）
ENUM_MAX_LEN = 32
# 表头探测扫描行数
HEADER_SCAN_ROWS = 5
# 字符串列长度分档
VARCHAR_SHORT = 64
VARCHAR_LONG = 128

# 日期形如 2026-08-02 / 2026-08-02 00:00:00 / 2026/8/2
_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?$")
_INT_RE = re.compile(r"^-?\d+$")
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")

# 序号类列名（Top/Bottom 无意义，宜作主键）
_SEQ_HEADER_NAMES = {"id", "no", "no.", "#", "rank", "index", "idx",
                     "序号", "编号", "代码", "code"}


# ==================== 基础判定（与 loader 同口径的本地实现）====================

def parse_number(value: str) -> Optional[float]:
    """把单元格文本解析为数值（容忍千分位逗号与 $ 符号）。

    与 app.ingestion.loader._parse_number 同口径；本地重实现以避免
    analytics 依赖 ingestion 的重依赖链。
    """
    if value is None:
        return None
    v = str(value).replace(",", "").replace("$", "").replace("￥", "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def is_sequence_col(header: str, values: Sequence[str]) -> bool:
    """判定是否为序号/ID 类列（可作为主键）。

    两个信号任一命中：
      1. 表头名为 id/no/序号 等；
      2. 值为连续不重复整数（distinct == 数量 且 max-min+1 == 数量，如 1..N）。
    """
    name = str(header).strip().lower()
    if name in _SEQ_HEADER_NAMES:
        return True

    nums = [parse_number(v) for v in values if str(v).strip()]
    if not nums or any(n is None for n in nums):
        return False
    ints = [int(n) for n in nums if float(n).is_integer()]
    if len(ints) != len(nums):
        return False
    distinct = set(ints)
    span = max(ints) - min(ints) + 1
    return len(distinct) == len(ints) and span == len(ints)


def looks_like_date(value: str) -> bool:
    return bool(_DATE_RE.match(str(value).strip()))


def normalize_date(value: str) -> Optional[str]:
    """把各种日期形态归一为 ISO `YYYY-MM-DD`（供 SQL DATE 列使用）。

    支持 `2026-08-02`、`2026-08-02 00:00:00`、`2026/8/2`、`2026/8/2 0:00`。
    解析失败返回 None（调用方决定该行是否作废）。
    """
    s = str(value).strip()
    if not s:
        return None
    if not _DATE_RE.match(s):
        return None
    # 归一：斜杠转横杠，去掉时间部分，补齐月日零填充
    date_part = re.split(r"[ T]", s, maxsplit=1)[0].replace("/", "-")
    parts = date_part.split("-")
    if len(parts) != 3:
        return None
    y, m, d = parts
    try:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except ValueError:
        return None


# ==================== 表头探测 ====================

def looks_like_header_row(row: Sequence[str]) -> bool:
    """该行是否"像表头"。

    判据（全部满足才算）：
      - 非空单元格 >= 2（大标题行通常只有 1 个非空）
      - 不含纯数值单元格（表头一般不是数字）
      - 无重复值
    """
    filled = [str(c).strip() for c in row if str(c).strip()]
    if len(filled) < 2:
        return False
    if any(_NUM_RE.match(c) for c in filled):
        return False
    if len(set(filled)) != len(filled):
        return False
    return True


def detect_header_row(rows: Sequence[Sequence[str]], scan: int = HEADER_SCAN_ROWS) -> int:
    """在首 N 行里找最像表头的一行，返回其下标；找不到返回 0。

    打分：非空单元格数最多者胜（并列取更靠前的行）。

    为什么必须做这件事：**实测算出的真实数据里就存在陷阱**
      - cost_data.xlsx：第 0 行是大标题（8 列只有 1 列有值），第 1 行才是表头
      - Employees：第 0 行就是表头
    而既有 RAG 通道的 loader 不做表头识别（第 0 行无条件当表头），
    如果 ETL 直接复用它的输出，cost_data 会建出一张只有 1 个有效列的表。
    """
    best_idx, best_score = 0, -1
    for i, row in enumerate(rows[:scan]):
        if not looks_like_header_row(row):
            continue
        score = len([c for c in row if str(c).strip()])
        if score > best_score:
            best_idx, best_score = i, score
    return best_idx


# ==================== 标识符与描述 ====================

_IDENT_CLEAN_RE = re.compile(r"[^0-9a-zA-Z_]+")


def make_identifier(raw_header: str, index: int, used: set[str]) -> str:
    """生成稳定、安全、唯一的物理列名。

    规则：
      - ASCII 表头（如 `First Name`）→ snake_case（`first_name`），可读性最好
      - 纯中文/无法转换的表头 → `col_{index}`（1 基），原表头保留在 description
      - 冲突时追加 `_2`、`_3`…

    安全性：结果只含 [a-z0-9_]，因此**天然免疫标识符注入**；
    但仍会经 sqlglot 的标识符转义再拼进 DDL（双保险）。
    """
    name = _IDENT_CLEAN_RE.sub("_", str(raw_header).strip().lower()).strip("_")
    # 折叠多个下划线
    name = re.sub(r"_{2,}", "_", name)

    # 纯 ASCII 且非空且以字母/下划线开头 → 可用
    if name and re.match(r"^[a-z_]", name):
        candidate = name
    else:
        candidate = f"col_{index + 1}"

    # 去重
    base = candidate
    n = 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    used.add(candidate)
    return candidate


def humanize_header(raw_header: str, index: int) -> str:
    """把物理表头还原成人类可读描述（进 COMMENT 与 prompt）。"""
    s = str(raw_header).strip()
    if not s:
        return f"第 {index + 1} 列"
    return s


# ==================== 推断主体 ====================

@dataclass
class InferredColumn:
    """一列的推断结果。"""

    name: str                     # 物理列名（安全标识符）
    sql_type: str                 # INTEGER / NUMERIC(14,2) / DATE / VARCHAR(n) / TEXT
    description: str              # 人类可读描述（原表头 + 推断线索）
    source_header: str            # 原始表头
    enum_values: List[str] = field(default_factory=list)
    is_primary_key: bool = False
    nullable: bool = True
    # 供调试与元数据落库
    null_count: int = 0
    distinct_count: int = 0
    samples: List[str] = field(default_factory=list)

    def comment(self) -> str:
        """生成 SQL COMMENT 文本（含枚举值内联）。

        枚举值内联是提升 Text2SQL 准确率**性价比最高**的手段：
        LLM 不必猜 'engineering' 还是 'Engineering'。
        """
        parts = [self.description]
        if self.enum_values:
            shown = self.enum_values[:ENUM_MAX_DISTINCT]
            parts.append(f"取值：{'/'.join(shown)}（共 {len(shown)} 类）")
        if self.is_primary_key:
            parts.append("主键")
        return "；".join(parts)


@dataclass
class InferredSchema:
    """一张表的推断结果。"""

    table_name: str
    display_name: str
    description: str
    columns: List[InferredColumn]
    header_row_index: int         # 表头所在行（数据从下一行开始）
    data_rows: int                # 数据行数（不含表头）
    header_row_skipped: List[List[str]] = field(default_factory=list)  # 被跳过的前导行

    @property
    def column_names(self) -> List[str]:
        return [c.name for c in self.columns]


def infer_column_type(values: Sequence[str], header: str) -> Tuple[str, bool]:
    """推断单列 SQL 类型。返回 (sql_type, is_primary_key)。

    全列判据（非抽样），优先级从窄到宽：
      1. 序号/ID 且为连续整数 → INTEGER PRIMARY KEY
      2. 全整数 → INTEGER
      3. 全数值 → NUMERIC(14,2)
      4. 全日期 → DATE
      5. 低基数短文本 → VARCHAR(64)（枚举值内联）
      6. 较长文本 → VARCHAR(128)
      7. 其余/含超长 → TEXT
    """
    non_empty = [str(v).strip() for v in values if str(v).strip()]

    # 空列 → TEXT（无法推断）
    if not non_empty:
        return "TEXT", False

    # 1) 主键候选
    if is_sequence_col(header, non_empty):
        return "INTEGER", True

    # 2) 全整数
    if all(_INT_RE.match(v) for v in non_empty):
        return "INTEGER", False

    # 3) 全数值（含小数）。注意 `parse_number` 容忍千分位与货币符号
    if all(_NUM_RE.match(v.replace(",", "").replace("$", "").replace("￥", "")) for v in non_empty):
        return "NUMERIC(14,2)", False

    # 4) 全日期
    if all(looks_like_date(v) for v in non_empty):
        return "DATE", False

    # 5) / 6) / 7) 文本分档
    max_len = max(len(v) for v in non_empty)
    distinct = len(set(non_empty))
    if distinct <= ENUM_MAX_DISTINCT and max_len <= ENUM_MAX_LEN:
        return f"VARCHAR({VARCHAR_SHORT})", False
    if max_len <= VARCHAR_LONG:
        return f"VARCHAR({VARCHAR_LONG})", False
    return "TEXT", False


def infer_schema(
    rows: Sequence[Sequence[str]],
    table_name: str,
    display_name: str = "",
    description: str = "",
    max_scan_rows: Optional[int] = None,
) -> InferredSchema:
    """从原始行推断整张表结构。

    Args:
        rows: 原始行（含表头行与可能的前导标题行），每行是字符串列表
        table_name: 物理表名（调用方已归一化）
        max_scan_rows: 只扫描前 N 行做类型推断（None = 全扫）。
            万行表全扫也可接受（纯字符串运算，实测很快），但提供该参数
            便于将来对超大表做抽样降级。
    """
    if not rows:
        return InferredSchema(table_name, display_name or table_name, description, [], 0, 0)

    header_idx = detect_header_row(rows)
    header = [str(c).strip() for c in rows[header_idx]]
    body = [list(r) for r in rows[header_idx + 1:]]
    skipped = [list(r) for r in rows[:header_idx]]

    # 统一列宽：以表头长度为准，短行补空、长行截断
    n_cols = len(header)
    norm_body: List[List[str]] = []
    for r in body:
        padded = [str(c).strip() if c is not None else "" for c in r[:n_cols]]
        padded += [""] * (n_cols - len(padded))
        # 全空行跳过
        if any(c for c in padded):
            norm_body.append(padded)

    scan = norm_body if max_scan_rows is None else norm_body[:max_scan_rows]

    used: set[str] = set()
    columns: List[InferredColumn] = []
    for ci, raw_header in enumerate(header):
        col_values = [r[ci] for r in scan]
        non_empty = [v for v in col_values if v]
        sql_type, is_pk = infer_column_type(col_values, raw_header)

        desc = humanize_header(raw_header, ci)
        # 标注推断线索，便于人工核对
        hints: List[str] = []
        if is_pk:
            hints.append("唯一序号")
        if not non_empty:
            hints.append("全列为空")
        if hints:
            desc = f"{desc}（{'、'.join(hints)}）"

        enum_values: List[str] = []
        if sql_type.startswith("VARCHAR"):
            seen: List[str] = []
            for v in non_empty:
                if v not in seen:
                    seen.append(v)
            enum_values = sorted(seen)

        columns.append(InferredColumn(
            name=make_identifier(raw_header, ci, used),
            sql_type=sql_type,
            description=desc,
            source_header=raw_header,
            enum_values=enum_values,
            is_primary_key=is_pk,
            nullable=len(non_empty) < len(col_values) or len(non_empty) == 0,
            null_count=len(col_values) - len(non_empty),
            distinct_count=len(set(non_empty)),
            samples=non_empty[:3],
        ))

    return InferredSchema(
        table_name=table_name,
        display_name=display_name or table_name,
        description=description,
        columns=columns,
        header_row_index=header_idx,
        data_rows=len(norm_body),
        header_row_skipped=skipped,
    )


# ==================== 表名归一化 ====================

_TABLE_CLEAN_RE = re.compile(r"[^0-9a-zA-Z_]+")


def normalize_table_name(source_file: str, sheet_name: str = "") -> str:
    """从文件名 + sheet 名生成安全表名。

    `xlsx-sample-large-10000-rows.xlsx` + `Employees` -> `employees`
    `xlsx-sample-multiple-sheets.xlsx` + `Sales`      -> `sales`
    `cost_data.xlsx` + `橱柜成本`                      -> `cost_data`

    规则：优先用 sheet 名（语义更强）；sheet 名为纯中文/不可转换时回退到
    文件名主干；最终保证只含 [a-z0-9_]、不以数字开头、长度受限。
    """
    def clean(s: str) -> str:
        s = _TABLE_CLEAN_RE.sub("_", str(s).strip().lower()).strip("_")
        return re.sub(r"_{2,}", "_", s)

    sheet_ident = clean(sheet_name)
    file_stem = clean(source_file.rsplit(".", 1)[0] if "." in source_file else source_file)

    # sheet 名可用则优先（Sales/Employees/Expenses 语义清晰）
    candidate = sheet_ident if (sheet_ident and sheet_ident[0].isalpha()) else ""

    if not candidate:
        # 回退：文件名主干去掉冗余的 xlsx-/sample- 前缀
        stem = re.sub(r"^(xlsx|sample)[_-]", "", file_stem)
        stem = re.sub(r"[_-]?sample[_-]?", "_", stem).strip("_")
        candidate = stem or file_stem or "table"

    if not candidate or not candidate[0].isalpha():
        candidate = f"t_{candidate}".strip("_") or "t_unnamed"

    return candidate[:48]
