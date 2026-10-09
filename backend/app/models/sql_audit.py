"""SQL 审计模型（P7）：记录每一次 LLM 生成的 SQL 执行。

为什么必须做（写入简历/面试也站得住的三条理由）：
1. **合规**：接入 Text2SQL 后，"谁在什么时候查了什么"是审计要求。
2. **追溯**：Agent 可能生成意料之外的查询，审计是唯一的复盘手段。
3. **成本与质量观测**：把耗时/行数/是否脱敏/是否被拦落库后，
   才能算"人均查询成本""SQL 拒绝率""脱敏触发次数"这类指标。

设计取舍：
- **只记 SQL 与元数据，不记结果集**。结果集可能含敏感数据，
  存下来等于把脱敏工作白做；元数据已足够审计与统计。
- 放在系统元数据库 `rag`（走 Alembic），与业务库物理隔离 ——
  审计表属于"系统能力"，且不应被只读角色看到。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SqlAudit(Base):
    """一次 SQL 执行的审计记录。"""

    __tablename__ = "sql_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 调用方标识（形如 "tenant:role"），便于按角色统计
    actor: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # 实际执行的 SQL（已过 guard 改写，含注入的 LIMIT）
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false", index=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 因权限不足被脱敏的列（JSONB 数组）—— 用于发现"有人在反复试探敏感数据"
    redacted_columns: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SqlAudit id={self.id} actor={self.actor!r} ok={self.ok}>"
