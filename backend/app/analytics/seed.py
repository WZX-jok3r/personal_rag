"""seed.py - 从知识库的 xlsx 装载业务表（ETL）。

流程：
    xlsx 文件
      -> openpyxl 读取每个 sheet（data_only=True，取公式的计算结果值）
      -> schema_infer 推断表头行与列类型（纯函数）
      -> ddl 建表（DROP + CREATE + COMMENT + 索引 + RLS）
      -> COPY 装载数据（万行级用 COPY，不用逐行 INSERT）
      -> 写元数据（analytics_tables / analytics_columns，落在 rag 库）

为什么 ETL 直接读原始文件，而不复用 RAG 通道 loader 的输出：
  ✅ 实测 loader.py:1415 把所有单元格 `str()` 化，类型信息已丢失，必须重新推断；
  ✅ loader 不做表头识别（第 0 行无条件当表头），会把 cost_data.xlsx 的
     大标题行当表头 → 建出只有 1 个有效列的表；
  ✅ 改造那个 1592 行的核心文件会触碰既有 RAG 链路与评测基线，风险远大于收益。
  取舍：同一份文件被读两遍（一次给 RAG、一次给 SQL），在一次性入库成本里可忽略，
  换来两条通道**完全解耦**、互不影响。

用法：
    python -m app.analytics.seed --all
    python -m app.analytics.seed --file knowledge_base/cost_data.xlsx
    python -m app.analytics.seed --all --verify     # 装载后跑真值断言
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import openpyxl
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.analytics import ddl
from app.analytics.schema_infer import (
    InferredSchema,
    infer_schema,
    normalize_date,
    normalize_table_name,
    parse_number,
)
from app.core.config import settings

logger = logging.getLogger(__name__)

# 默认要装载的文件（相对 knowledge_base）
DEFAULT_FILES = [
    "xlsx-sample-large-10000-rows.xlsx",
    "xlsx-sample-multiple-sheets.xlsx",
    "cost_data.xlsx",
]

# 不建表的 sheet：这类 sheet 是人工写的汇总页（Metric/Value 两列），
# 不是可统计的数据集；建表没有意义，还会污染 schema 让模型误选。
_SKIP_SHEETS = {"summary", "汇总", "说明", "readme"}

# 表的中文语义（进 prompt 帮助模型判断"该不该用这张表"）
_TABLE_DISPLAY = {
    "employees": ("员工表", "全公司员工花名册：含部门、薪资与入职日期，适合做人力统计"),
    "sales": ("销售流水", "逐笔产品销售记录：含日期、产品、销量与营收，适合做销售统计"),
    "expenses": ("费用支出", "逐笔费用记录：含日期、类别与金额，适合做费用统计"),
    "cost_data": ("橱柜成本明细", "装修/家具项目的逐项成本（人民币计价），含单价、数量与计费方式"),
}


# ==================== 读写工具 ====================

def _cell_to_str(v: Any) -> str:
    """单元格 -> 字符串。

    与 loader 的 `str(cell).strip()` 保持同口径（便于两边结果可对照），
    但额外把 datetime 归一成日期串，减少下游类型推断的分支。
    """
    if v is None:
        return ""
    if hasattr(v, "strftime"):          # datetime / date
        return v.strftime("%Y-%m-%d")
    return str(v).strip()


def read_sheets(path: Path) -> List[tuple[str, List[List[str]]]]:
    """读取 xlsx 的所有 sheet，返回 [(sheet_name, rows)]（rows 已转字符串）。"""
    wb = openpyxl.load_workbook(path, data_only=True)
    out: List[tuple[str, List[List[str]]]] = []
    try:
        for ws in wb.worksheets:
            rows: List[List[str]] = []
            for row in ws.iter_rows(values_only=True):
                cleaned = [_cell_to_str(c) for c in row]
                if any(c for c in cleaned):        # 跳过全空行
                    rows.append(cleaned)
            if rows:
                out.append((ws.title, rows))
    finally:
        wb.close()
    return out


def coerce_value(raw: str, sql_type: str) -> Optional[str]:
    """按目标类型把字符串转成可 COPY 的字面量；无法转换返回 None（→ NULL）。"""
    s = str(raw).strip()
    if not s:
        return None
    if sql_type.startswith("NUMERIC"):
        n = parse_number(s)
        return None if n is None else repr(n)
    if sql_type == "INTEGER":
        n = parse_number(s)
        if n is None:
            return None
        # 整数列遇到小数（数据脏）时取整，避免 COPY 整批失败
        return str(int(n))
    if sql_type == "DATE":
        return normalize_date(s)
    return s


# ==================== 装载 ====================

@dataclass
class LoadResult:
    table_name: str
    source: str
    sheet_name: str
    row_count: int
    column_count: int
    header_row_index: int
    loaded: int = 0
    skipped: int = 0
    columns: List[str] = field(default_factory=list)


def _copy_rows(engine: Engine, schema: InferredSchema, rows: List[List[str]]) -> tuple[int, int]:
    """用 COPY 批量装载。返回 (成功行数, 跳过行数)。

    用 COPY 而不是逐行 INSERT：万行级差距是秒级 vs 分钟级。
    通过 psycopg 的 copy 接口流式写入，避免把整个数据集拼成巨型 SQL。
    """
    table = ddl.q(schema.table_name)
    cols = [ddl.q(c.name) for c in schema.columns] + [ddl.q(ddl.TENANT_COLUMN)]
    col_list = ", ".join(cols)

    loaded = skipped = 0
    raw_conn = engine.raw_connection()
    try:
        with raw_conn.cursor() as cur:
            # 先清空（重灌语义；DROP/CREATE 已在上一步做过，这里兜底）
            cur.execute(f"TRUNCATE TABLE {table}")
            with cur.copy(
                f"COPY {table} ({col_list}) FROM STDIN"
            ) as copy:
                for row in rows:
                    # 统一列宽
                    padded = list(row[: len(schema.columns)])
                    padded += [""] * (len(schema.columns) - len(padded))
                    values: List[Optional[str]] = []
                    bad = False
                    for cell, col in zip(padded, schema.columns):
                        v = coerce_value(cell, col.sql_type)
                        # 非空原值转不出目标类型 → 视为脏数据，跳过整行
                        if v is None and str(cell).strip():
                            bad = True
                            break
                        values.append(v)
                    if bad:
                        skipped += 1
                        continue
                    values.append(ddl.DEFAULT_TENANT)
                    copy.write_row(values)
                    loaded += 1
        raw_conn.commit()
    except Exception:
        raw_conn.rollback()
        raise
    finally:
        raw_conn.close()
    return loaded, skipped


def _upsert_metadata(
    rag_engine: Engine,
    schema: InferredSchema,
    source: str,
    sheet_name: str,
    row_count: int,
) -> None:
    """写元数据到 rag 库（analytics_tables / analytics_columns）。"""
    from app.models.analytics import AnalyticsColumn, AnalyticsTable

    display, desc = _TABLE_DISPLAY.get(schema.table_name, (schema.display_name, schema.description))

    with Session(rag_engine) as s:
        existing = s.query(AnalyticsTable).filter_by(table_name=schema.table_name).one_or_none()
        if existing is not None:
            # 重灌：删旧行（级联删 columns）再建，避免列结构变化后残留旧列
            s.delete(existing)
            s.flush()

        tbl = AnalyticsTable(
            table_name=schema.table_name,
            display_name=display,
            description=desc,
            source=source,
            sheet_name=sheet_name,
            row_count=row_count,
            is_enabled=True,
        )
        s.add(tbl)
        s.flush()

        for ordinal, col in enumerate(schema.columns):
            s.add(AnalyticsColumn(
                table_id=tbl.id,
                ordinal=ordinal,
                column_name=col.name,
                data_type=col.sql_type,
                description=col.comment(),
                enum_values=col.enum_values[:50] if col.enum_values else None,
                is_primary=col.is_primary_key,
                is_nullable=col.nullable,
            ))
        s.commit()


def load_file(
    analytics_engine: Engine,
    rag_engine: Engine,
    path: Path,
    skip_sheets: Optional[set[str]] = None,
) -> List[LoadResult]:
    """把一个 xlsx 的所有 sheet 装载成表。"""
    skip = _SKIP_SHEETS if skip_sheets is None else skip_sheets
    results: List[LoadResult] = []

    for sheet_name, rows in read_sheets(path):
        if sheet_name.strip().lower() in skip:
            logger.info("[seed] 跳过汇总类 sheet: %s / %s", path.name, sheet_name)
            continue

        table_name = normalize_table_name(path.name, sheet_name)
        schema = infer_schema(
            rows,
            table_name=table_name,
            display_name=sheet_name,
            description=f"{path.name} 的 {sheet_name} 工作表",
        )
        if not schema.columns:
            logger.warning("[seed] %s / %s 无有效列，跳过", path.name, sheet_name)
            continue

        ddl.create_or_replace_table(analytics_engine, schema)
        loaded, skipped_rows = _copy_rows(analytics_engine, schema, rows[schema.header_row_index + 1:])

        _upsert_metadata(rag_engine, schema, path.name, sheet_name, loaded)

        res = LoadResult(
            table_name=schema.table_name,
            source=path.name,
            sheet_name=sheet_name,
            row_count=loaded,
            column_count=len(schema.columns),
            header_row_index=schema.header_row_index,
            loaded=loaded,
            skipped=skipped_rows,
            columns=schema.column_names,
        )
        results.append(res)
        logger.info(
            "[seed] %s <- %s/%s | 表头行=%d | %d 列 | 装载 %d 行（跳过 %d）",
            schema.table_name, path.name, sheet_name,
            schema.header_row_index, len(schema.columns), loaded, skipped_rows,
        )
    return results


# ==================== 供 worker 调用的入口（P7 补齐）====================

def should_sync_to_analytics(path: Path) -> bool:
    """判断该文件是否应同步到 SQL 侧。

    只有表格类文件有意义（xlsx/xlsm）；PDF/DOCX 的表格提取是启发式的、
    跨页会截断，用它建表会引入静默错误 —— 这是本项目刻意划定的数据源边界
    （见 docs/text2sql-数据层设计.md §5.0）。
    """
    return path.suffix.lower() in {".xlsx", ".xlsm"}


def sync_analytics_tables(path: Path) -> List[LoadResult]:
    """把单个文件的表格同步成 SQL 表（供 ARQ worker 在入库流程中调用）。

    ## 为什么需要这个函数（一段被漏掉的实现）

    设计文档《text2sql-数据层设计.md》明确写了「复用现有异步入库链路，
    把『表格转表』做成一个 ARQ 任务」，并画出：

        POST /documents 上传 xlsx
          └─ worker: ingest_document
              ├─ [现有] 解析 → 分块 → 向量化 → Qdrant
              └─ [新增] tabular_detect → 类型推断 → CREATE TABLE / COPY

    **但实际只交付了手动 CLI（`python -m app.analytics.seed`），worker 从未接线。**
    后果：通过 API 上传的 xlsx **不会**出现在 SQL 侧 ——
    RAG 能检索到、SQL 查不到，两条通道静默不一致。
    本函数即为补上这一环。

    ## 设计取舍

    - **只处理 xlsx**（`should_sync_to_analytics` 判定），与既有数据源边界一致
    - **幂等**：`create_or_replace_table` 先 DROP 再建，重灌安全
    - **不负责建库/建角色**：那是运维动作（`app.analytics.setup_db`），
      部署时执行一次。此处若库不存在会抛异常，由调用方决定如何处理
    - 返回每个 sheet 的装载结果，便于 worker 记录与前端展示
    """
    analytics_engine = create_engine(settings.sync_analytics_url)
    rag_engine = create_engine(settings.sync_postgres_url)
    try:
        return load_file(analytics_engine, rag_engine, path)
    finally:
        analytics_engine.dispose()
        rag_engine.dispose()


# ==================== 真值自检 ====================

# 用 pandas/openpyxl 直读原始文件算出的真值（见改造方案第三部分）。
#
# ⚠️ cost_data.xlsx 的真实结构（实测，2026-10-08）：
#     [0] 标题行  「橱柜项目成本明细表」（8 列只有 1 列有值）-> 由表头探测跳过
#     [1] 表头行  「项目/品牌型号/.../备注」
#     [2..6] 数据 5 行
#     [7] 尾部汇总脚注「汇总：铰链(28×20)、颗粒板...」（首列有值 => openpyxl 视为数据行）
#   => 装载后 **6 行**（含 [7] 这行脚注），不是改造方案文档里估的 7 行。
#   列数 = 7 业务列 + tenant_id = **8**。
TRUTH_CHECKS: List[tuple[str, str, Any]] = [
    ("employees", "SELECT count(*) FROM employees", 10000),
    ("employees", "SELECT count(*) FROM employees WHERE department='Sales'", 1042),
    ("employees", "SELECT count(*) FROM employees WHERE department='Engineering'", 1010),
    ("employees", "SELECT count(*) FROM employees WHERE hire_date >= '2026-01-01'", 192),
    ("employees", "SELECT department FROM employees GROUP BY department ORDER BY count(*) DESC LIMIT 1", "Sales"),
    ("employees", "SELECT department FROM employees GROUP BY department ORDER BY count(*) ASC LIMIT 1", "Finance"),
    # 这两条是 30 倍错误案例的直接回归断言（改造方案第三部分）
    ("employees",
     "SELECT count(*) FROM employees WHERE department='Sales'", 1042),
    ("sales", "SELECT count(*) FROM sales", 20),
    ("expenses", "SELECT count(*) FROM expenses", 15),
    ("cost_data", "SELECT count(*) FROM cost_data", 6),
    # cost_data 必须建出 8 业务列（不是 1 列）—— 表头陷阱的回归断言。
    # 物理列 = 8 业务列 + tenant_id = 9。
    ("cost_data",
     "SELECT count(*) FROM information_schema.columns "
     "WHERE table_schema='public' AND table_name='cost_data'", 9),
    # 中文列名表必须能按数值列排序（验证中文表头没有破坏列语义）
    ("cost_data",
     "SELECT col_1 FROM cost_data WHERE col_4 IS NOT NULL ORDER BY col_4 DESC LIMIT 1", "PET肤感门板"),
]


def verify_truth(engine: Engine) -> tuple[bool, List[str]]:
    """装载后用 SQL 实测真值。这是"SQL 通道真的能答对统计题"的第一道证明。"""
    problems: List[str] = []
    with engine.connect() as conn:
        for table, sql, expected in TRUTH_CHECKS:
            # 表不存在时给明确提示，而不是抛异常
            if not ddl.table_exists(engine, table):
                problems.append(f"[{table}] 表不存在")
                continue
            try:
                got = conn.execute(text(sql)).scalar()
            except Exception as e:  # noqa: BLE001
                problems.append(f"[{table}] 查询失败: {str(e)[:120]}")
                continue
            if got != expected:
                problems.append(f"[{table}] {sql[:70]}... 期望 {expected!r} 得到 {got!r}")
    return (len(problems) == 0), problems


# ==================== CLI ====================

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="从 xlsx 装载 Analytics 业务表（ETL）")
    parser.add_argument("--all", action="store_true", help="装载默认的全部 xlsx")
    parser.add_argument("--file", action="append", default=[], help="指定文件（可多次）")
    parser.add_argument("--verify", action="store_true", help="装载后跑真值断言")
    args = parser.parse_args()

    if not args.all and not args.file:
        parser.error("需指定 --all 或 --file")

    targets: List[Path] = []
    if args.all:
        targets += [settings.knowledge_base_dir / f for f in DEFAULT_FILES]
    for f in args.file:
        p = Path(f)
        targets.append(p if p.is_absolute() else (settings.knowledge_base_dir / p.name))

    missing = [p for p in targets if not p.exists()]
    if missing:
        for p in missing:
            print(f"[seed] 文件不存在: {p}", file=sys.stderr)
        return 1

    analytics_engine = create_engine(settings.sync_analytics_url)
    rag_engine = create_engine(settings.sync_postgres_url)

    all_results: List[LoadResult] = []
    try:
        for p in targets:
            all_results += load_file(analytics_engine, rag_engine, p)

        print()
        print("=" * 78)
        print(f"装载完成：{len(all_results)} 张表")
        print("=" * 78)
        print(f"{'表名':<14}{'来源':<38}{'sheet':<12}{'表头行':>6}{'列':>4}{'行数':>8}")
        print("-" * 78)
        for r in all_results:
            print(f"{r.table_name:<14}{r.source:<38}{r.sheet_name:<12}"
                  f"{r.header_row_index:>6}{r.column_count:>4}{r.row_count:>8}")

        if args.verify:
            ok, problems = verify_truth(analytics_engine)
            print()
            print("=" * 78)
            print(f"真值自检: {'全部通过 ✅' if ok else '失败 ❌'}")
            print("=" * 78)
            for pr in problems:
                print(f"  - {pr}")
            return 0 if ok else 1
    finally:
        analytics_engine.dispose()
        rag_engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
