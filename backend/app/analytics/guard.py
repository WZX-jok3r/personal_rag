"""guard.py - SQL 安全网关的第一层：AST 白名单校验 + LIMIT 强制注入。

⚠️ 设计立场（必须先读，否则会误用本模块）：

    📄 sqlglot 官方 FAQ 原文：
      "The parser is intentionally lenient, so it can accept queries that a real
       engine would reject. **SQLGlot is a transpiler, not a validator.**
       A query that parses successfully may still fail at execution time."

    → **连 sqlglot 官方都否认它是安全边界**。因此本模块**只是四层防御的第一层**，
      绝不能作为唯一防线。完整四层（见 docs/text2sql-数据层设计.md 第二节）：

        ① 本模块：AST 白名单（快、明确拒绝非 SELECT）
        ② executor.py：只读事务 + statement_timeout + 结果行数上限
        ③ 数据库角色：kb_ro 只有 GRANT SELECT + default_transaction_read_only
        ④ RLS：app.tenant_id 行级隔离

      任何一层单独都能被绕过：
        - 只靠 ① → 方言差异/解析宽松可能放过；官方自称非 validator
        - 只靠 ② → PostgreSQL 官方承认只读是"high-level notion"，不阻止所有落盘写
        - 只靠 ③ → 挡不住"用合法 SELECT 读敏感数据"（如全公司薪资）
        - 只靠 ④ → 若忘了 SET 变量则策略失效

为什么用 sqlglot 而不是 sqlparse：
    sqlparse 官方自我定位就是 "non-validating SQL parser… accepts any input
    without validating it"，且无任何访问控制特性、比 sqlglot 慢 4~20 倍。
    它只适合格式化，**绝不可用作安全边界**。

防护清单（每条都对应 tests/test_sql_guard.py 里的攻击载荷）：
    - 非 SELECT 语句（INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE/GRANT/COPY/CALL…）
    - 多语句（`SELECT 1; DROP TABLE x`）
    - 系统目录访问（pg_catalog / information_schema / pg_shadow…）
    - `SELECT ... INTO`（写表）
    - 文件读取（pg_read_file / pg_ls_dir）
    - 命令执行（COPY ... TO PROGRAM）
    - 注释混淆（`/*x*/ DELETE /*y*/ FROM t`）
    - 危险函数（pg_sleep 做 DoS、dblink/lo_import 等）
    - 强制 LIMIT 注入（不信 LLM 自己写的 LIMIT）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Set

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

logger = logging.getLogger(__name__)

DIALECT = "postgres"

# ---- 禁止的语句类型（根节点或子树出现即拒绝）----
# 说明：用元组而非集合，因为要 isinstance 检查
FORBIDDEN_STATEMENTS = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge,
    exp.Drop, exp.Create, exp.Alter, exp.TruncateTable,
    exp.Grant, exp.Copy, exp.Command,
    exp.Into,          # SELECT ... INTO newtable
    exp.Set,           # SET / SET ROLE
    exp.Use,
    exp.Transaction,   # BEGIN/COMMIT
    exp.Commit,
    exp.Rollback,
)

# ---- 禁止访问的系统 schema ----
FORBIDDEN_SCHEMAS: Set[str] = {
    "pg_catalog", "information_schema", "pg_toast", "pg_temp",
}

# ---- 禁止的函数（读文件 / 命令执行 / DoS / 大对象 / 外部连接）----
FORBIDDEN_FUNCTIONS: Set[str] = {
    # 文件系统
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "pg_read_server_files", "pg_write_server_files",
    # 大对象 / 服务端文件
    "lo_import", "lo_export", "pg_import_system_collations",
    # 外部连接（数据外带）
    "dblink", "dblink_connect", "postgres_fdw_disconnect",
    # DoS：合法 SELECT 也能打满 CPU/挂住连接
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    # 事务/权限
    "set_config", "pg_advisory_lock", "pg_advisory_xact_lock",
    # 复制/逻辑解码
    "pg_logical_emit_message", "pg_create_logical_replication_slot",
}

# 禁止的列名（访问即拒绝）—— 兜住 pg_shadow/pg_authid 这类账号表
FORBIDDEN_TABLE_NAMES: Set[str] = {
    "pg_shadow", "pg_authid", "pg_roles", "pg_user", "pg_group",
    "pg_stat_activity", "pg_settings", "pg_file_settings",
    "pg_hba_file_rules", "pg_config",
}


@dataclass
class GuardResult:
    """校验结果。"""

    ok: bool
    sql: str                      # ok=True 时为改写后的 SQL（已注入 LIMIT）
    reason: str = ""              # ok=False 时的拒绝原因（面向运维/审计）
    user_message: str = ""        # ok=False 时面向 LLM 的说明（用于自修正回灌）
    # 改写信息
    limit_injected: bool = False


# ==================== 内部检查 ====================

def _collect_forbidden_functions(tree: exp.Expression) -> List[str]:
    found: List[str] = []
    for node in tree.walk():
        if isinstance(node, (exp.Anonymous, exp.Func)):
            name = ""
            if isinstance(node, exp.Anonymous):
                name = str(node.this or "")
            else:
                # sqlglot 把已知函数解析为具体类，用 sql_name() 取名字
                try:
                    name = node.sql_name()
                except Exception:  # noqa: BLE001
                    name = type(node).__name__
            if name and name.lower() in FORBIDDEN_FUNCTIONS:
                found.append(name.lower())
    return found


def _collect_forbidden_tables(tree: exp.Expression) -> List[str]:
    """检查表名与 schema 是否命中黑名单。"""
    found: List[str] = []
    for node in tree.find_all(exp.Table):
        # node.name 是表名；node.db 是 schema 限定
        tname = (node.name or "").lower()
        db = (node.db or "").lower()
        if tname in FORBIDDEN_TABLE_NAMES:
            found.append(tname)
        if db in FORBIDDEN_SCHEMAS:
            found.append(f"{db}.{tname}")
    return found


def _inject_limit(tree: exp.Expression, max_rows: int) -> tuple[exp.Expression, bool]:
    """强制 LIMIT：**外层包裹**，而不是往原 SQL 尾部追加。

    为什么必须包裹而不是追加：
      - 追加会与 LLM 已写的 `LIMIT 999999` 冲突（变成两个 LIMIT → 语法错误）；
      - 追加无法处理 `UNION` 等结构（LIMIT 作用范围不明确）。
    包裹成 `SELECT * FROM (<original>) AS _q LIMIT n` 后，
    无论原 SQL 内部写了什么 LIMIT，外层都把它压到 max_rows —— **上界不可绕过**。

    ⚠️ 为什么注入的是 max_rows + 1 而不是 max_rows：
      如果恰恰注入 max_rows，执行器读到 max_rows 行时就**无法区分**
      「结果总共就这么多」与「被上限截断了」。我们靠"多读一行"来判断截断，
      所以注入的上限必须比读取上限多 1。
      这两个常量必须严格满足 `GUARD_LIMIT == max_rows + 1`，
      否则截断标志会**静默失真**（把截断结果当成完整结果汇报给用户）。
      已有单测 test_guard_limit_matches_executor_probe 锁死这个不变式。
    """
    wrapped = exp.select("*").from_(tree.subquery("_q")).limit(max_rows + 1)
    return wrapped, True


# ==================== 主入口 ====================

def guard_sql(raw_sql: str, max_rows: int) -> GuardResult:
    """校验并改写 LLM 生成的 SQL。

    Args:
        raw_sql: 模型生成的原始 SQL（可能包含 markdown 代码块包裹）
        max_rows: 允许返回的最大行数（超出即截断）

    Returns:
        GuardResult。ok=True 时 `sql` 字段是**可直接执行**的改写后语句。
    """
    if not raw_sql or not raw_sql.strip():
        return GuardResult(False, "", "空 SQL", "生成的 SQL 为空，请重新生成一条 SELECT 语句。")

    sql = raw_sql.strip()

    # 容错：剥掉模型常加的 markdown 代码围栏
    if sql.startswith("```"):
        lines = sql.splitlines()
        # 去掉首行 ``` 或 ```sql
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # 去掉结尾 ```
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        sql = "\n".join(lines).strip()

    # ---- 1) 解析（语法层）----
    try:
        statements = sqlglot.parse(sql, dialect=DIALECT)
    except (ParseError, TokenError) as e:
        return GuardResult(
            False, "", f"SQL 解析失败: {e}",
            f"SQL 语法有误，无法解析：{str(e)[:200]}。请修正语法后重新生成。",
        )
    except Exception as e:  # noqa: BLE001
        return GuardResult(
            False, "", f"SQL 解析异常: {type(e).__name__}: {e}",
            f"SQL 解析异常：{str(e)[:200]}。请重新生成一条简单的 SELECT 语句。",
        )

    # 过滤 None（空语句，如末尾分号）
    statements = [s for s in statements if s is not None]

    # ---- 2) 只允许单条语句 ----
    if len(statements) != 1:
        return GuardResult(
            False, "", f"拒绝多语句（检测到 {len(statements)} 条）",
            "检测到多条 SQL 语句。只允许执行单条 SELECT 查询，请把多条合并为一条。",
        )

    tree = statements[0]

    # ---- 3) 根节点必须是 SELECT ----
    if not isinstance(tree, exp.Select):
        return GuardResult(
            False, "", f"根节点非 SELECT 而是 {type(tree).__name__}",
            f"只允许 SELECT 查询，检测到 {type(tree).__name__.upper()}。请改写为 SELECT 语句。",
        )

    # ---- 3b) 结构完整性自检 ----
    # 背景：sqlglot 官方声明"The parser is intentionally lenient, so it can accept
    # queries that a real engine would reject"。实测确证：裸 `SELECT`（无投影、无 FROM）
    # 能被解析成合法的 exp.Select，但外层包裹后会生成
    # `SELECT * FROM (SELECT) AS _q LIMIT 200` —— 这是**语法非法的 SQL**，
    # 会以数据库报错的形式暴露，而不是被安全网关干净地拒绝。
    # 因此这里补一道结构检查：既无投影列、又无 FROM 子句 => 视为无效查询。
    has_projection = bool(tree.expressions)
    has_from = tree.args.get("from") is not None or tree.find(exp.From) is not None
    if not has_projection and not has_from:
        return GuardResult(
            False, "", "SELECT 结构不完整（无投影列且无 FROM 子句）",
            "SQL 结构不完整：SELECT 既没有要查询的列，也没有指定数据表。"
            "请生成完整语句，例如 SELECT count(*) FROM employees。",
        )

    # ---- 4) 子树不得含任何禁止语句 ----
    for node in tree.walk():
        for forbidden in FORBIDDEN_STATEMENTS:
            if isinstance(node, forbidden):
                name = type(node).__name__.upper()
                return GuardResult(
                    False, "", f"含禁止语句类型 {name}",
                    f"检测到 {name} 等写操作或非查询语句。本系统只读，只允许 SELECT。",
                )

    # ---- 5) 系统目录 / 敏感表 ----
    bad_tables = _collect_forbidden_tables(tree)
    if bad_tables:
        return GuardResult(
            False, "", f"访问系统表/敏感表: {', '.join(sorted(set(bad_tables)))}",
            "不允许访问系统目录或账号权限表。请只查询业务数据表。",
        )

    # ---- 6) 危险函数 ----
    bad_funcs = _collect_forbidden_functions(tree)
    if bad_funcs:
        return GuardResult(
            False, "", f"调用禁止函数: {', '.join(sorted(set(bad_funcs)))}",
            f"不允许调用函数 {', '.join(sorted(set(bad_funcs)))}。请改用普通聚合/条件查询。",
        )

    # ---- 7) 强制 LIMIT 注入 ----
    guarded, injected = _inject_limit(tree, max_rows)

    try:
        final_sql = guarded.sql(dialect=DIALECT)
    except Exception as e:  # noqa: BLE001
        return GuardResult(
            False, "", f"改写回写失败: {e}",
            "SQL 改写失败，请重新生成一条更简单的 SELECT 语句。",
        )

    return GuardResult(True, final_sql, "", "", limit_injected=injected)


def is_safe(sql: str, max_rows: int = 200) -> bool:
    """便捷判断（供测试与快速自检使用）。"""
    return guard_sql(sql, max_rows).ok
