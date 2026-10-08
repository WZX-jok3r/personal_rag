"""ddl.py - 业务表建表（在 kb_analytics 库，幂等）。

设计取舍：
- **业务表不走 Alembic**。理由：业务表随数据源变化（上传一个新 xlsx 就可能多一张表），
  若走 Alembic 就变成"上传一个文件生成一个迁移文件"，运维上不可接受。
  元数据两表（analytics_tables/columns）走 Alembic，因为它们描述的是系统能力。
- **表结构与数据装载同源**：都由 schema_infer 的推断结果驱动，
  避免"DDL 手写、装载按另一套规则"导致列错位。
- **标识符一律经 sqlglot 转义**。虽然 schema_infer 产出的标识符已只含 [a-z0-9_]，
  但仍走转义（双保险），确保任何情况下都不会因标识符拼接而注入。

RLS 设计（多租户）：
  业务表带 `tenant_id`，启用 RLS 并绑定 `app.tenant_id` 会话变量。
  策略写成 **未设置变量时放行全部**，原因：
    - 演示语料是全公司共享数据（不属于某个租户），若默认拒绝会导致什么都查不到；
    - "未认证 = 见全部" 与既有 RAG 通道的 `auth_enabled=False` 软关闭语义一致；
    - 一旦设置了变量（认证用户），就**只**能看到本租户数据 —— 这是硬隔离。
  这个取舍必须显式记录，因为它与"默认拒绝"的安全直觉相反。
"""

from __future__ import annotations

import logging
from typing import List

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlglot import exp

from app.analytics.schema_infer import InferredSchema

logger = logging.getLogger(__name__)

# 多租户列名与 RLS 会话变量（与既有 RAG 通道的 tenant_field 语义保持一致）
TENANT_COLUMN = "tenant_id"
TENANT_GUC = "app.tenant_id"

# 演示数据的默认租户：语料是全公司共享数据，未认证时归属此值
DEFAULT_TENANT = "a"


def q(identifier: str) -> str:
    """把标识符转义为 PostgreSQL 双引号形式（防注入）。"""
    return exp.to_identifier(str(identifier), quoted=True).sql(dialect="postgres")


def build_create_table_sql(schema: InferredSchema, tenant_column: bool = True) -> str:
    """由推断结果生成 CREATE TABLE IF NOT EXISTS 语句。

    列定义来自 schema.columns；主键取第一个 is_primary_key 的列，
    若没有主键列则不加主键约束（不伪造 ID —— 伪造会掩盖数据问题）。
    """
    lines: List[str] = []
    pk_cols: List[str] = []

    for col in schema.columns:
        parts = [f"  {q(col.name)} {col.sql_type}"]
        if col.is_primary_key:
            parts.append("NOT NULL")
            pk_cols.append(col.name)
        lines.append(" ".join(parts))

    if tenant_column:
        lines.append(f'  {q(TENANT_COLUMN)} VARCHAR(64) NOT NULL DEFAULT \'{DEFAULT_TENANT}\'')

    if pk_cols:
        lines.append(f"  PRIMARY KEY ({', '.join(q(c) for c in pk_cols)})")

    body = ",\n".join(lines)
    return f"CREATE TABLE IF NOT EXISTS {q(schema.table_name)} (\n{body}\n)"


def build_comments_sql(schema: InferredSchema) -> List[str]:
    """生成 COMMENT ON 语句（表 + 列）。

    COMMENT 是 PostgreSQL schema introspection 的一等公民，
    也是喂给 LLM 的「数据字典」的原始来源 —— 枚举值内联就在这里落地。
    """
    stmts: List[str] = []
    table_comment = schema.description or schema.display_name
    if table_comment:
        escaped = table_comment.replace("'", "''")
        stmts.append(f"COMMENT ON TABLE {q(schema.table_name)} IS '{escaped}'")

    for col in schema.columns:
        comment = col.comment().replace("'", "''")
        stmts.append(f"COMMENT ON COLUMN {q(schema.table_name)}.{q(col.name)} IS '{comment}'")

    # 数值列建索引（LLM 高频做聚合/排序/过滤）
    for col in schema.columns:
        if col.sql_type.startswith(("INTEGER", "NUMERIC", "DATE")) or col.is_primary_key:
            stmts.append(
                f"CREATE INDEX IF NOT EXISTS {q(f'ix_{schema.table_name}_{col.name}')} "
                f"ON {q(schema.table_name)} ({q(col.name)})"
            )
    return stmts


def build_rls_sql(table_name: str) -> List[str]:
    """启用 RLS 并绑定 app.tenant_id 会话变量。

    策略语义（重要，见模块 docstring）：
      - `app.tenant_id` 未设置（NULL）→ 放行全部行（匿名/评测/演示）
      - `app.tenant_id` 已设置        → 只放行 tenant_id 匹配的行（硬隔离）
    """
    return [
        f"ALTER TABLE {q(table_name)} ENABLE ROW LEVEL SECURITY",
        # 幂等：先删后建
        f"DROP POLICY IF EXISTS {q(f'tenant_isolation_{table_name}')} ON {q(table_name)}",
        (
            f"CREATE POLICY {q(f'tenant_isolation_{table_name}')} ON {q(table_name)} "
            f"USING ("
            f"  current_setting('{TENANT_GUC}', true) IS NULL "
            f"  OR current_setting('{TENANT_GUC}', true) = '' "
            f"  OR {q(TENANT_COLUMN)} = current_setting('{TENANT_GUC}', true)"
            f")"
        ),
    ]


def create_or_replace_table(engine: Engine, schema: InferredSchema) -> None:
    """建表 + 注释 + 索引 + RLS。**会先 DROP 再建**（重灌语义）。"""
    with engine.begin() as conn:
        # 重灌语义：先删旧表，保证列结构变化时不会残留旧列
        conn.execute(text(f"DROP TABLE IF EXISTS {q(schema.table_name)} CASCADE"))
        conn.execute(text(build_create_table_sql(schema)))
        for stmt in build_comments_sql(schema):
            conn.execute(text(stmt))
        for stmt in build_rls_sql(schema.table_name):
            conn.execute(text(stmt))
    logger.info("[ddl] 已重建表 %s（%d 列）", schema.table_name, len(schema.columns))


def table_exists(engine: Engine, table_name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=:n"
            ),
            {"n": table_name},
        ).first()
    return row is not None
