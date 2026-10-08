"""Analytics 元数据模型：登记「哪些业务表可被 LLM 看到」及其列语义。

为什么需要这张元数据表（而不是运行时 introspect 业务库）：
1. **它是 prompt 里 schema 描述的唯一来源** —— 快速、可控、不依赖每次查询
   都去 information_schema 转一圈；
2. **它是表级白名单** —— `is_enabled=False` 即让某张表对 LLM 不可见，
   实现"数据权限"而非"表权限"；
3. **它让人工校正成为可能** —— 自动推断的列名/语义可能不理想，
   直接改元数据即可，不必改代码或重灌数据。

注意：这两张表建在**系统元数据库 `rag`**（走 Alembic 管理），
而不是业务库 `kb_analytics`：
- 它们是「系统如何理解业务数据」的元信息，与业务数据本身的演进节奏不同；
- 复用既有 Alembic/ORM/仓储体系，不引入第二套迁移机制。

安全说明：本表**不存业务数据本身**，只存结构描述与少量样例值。
`enum_values` 会进 SQL COMMENT 与 LLM prompt，因此它是"有意公开给模型"的信息；
若某列的取值属于敏感数据，应在元数据里清空 enum_values（而不是指望模型不看）。
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class AnalyticsTable(Base, TimestampMixin):
    """一张可被 Text2SQL 查询的业务表。"""

    __tablename__ = "analytics_tables"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 业务库中的物理表名（已在生成时归一化为安全标识符）
    table_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 人类可读名，如「员工表」——进 prompt，模型据此匹配中文问法
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 业务语义描述，写进 prompt 帮助模型判断"该不该用这张表"
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 来源文件与 sheet，便于追溯与重灌
    source: Mapped[str] = mapped_column(String(512), nullable=False)
    sheet_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 该表的归属租户；NULL 表示共享（所有租户可见）
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # 表级白名单开关：False 即从 prompt 中消失
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )

    columns: Mapped[List["AnalyticsColumn"]] = relationship(
        back_populates="table",
        cascade="all, delete-orphan",
        order_by="AnalyticsColumn.ordinal",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AnalyticsTable {self.table_name!r} rows={self.row_count}>"


class AnalyticsColumn(Base, TimestampMixin):
    """业务表中的一列及其语义。"""

    __tablename__ = "analytics_columns"
    __table_args__ = (
        UniqueConstraint("table_id", "column_name", name="uq_analytics_columns_table_id_column_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    table_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("analytics_tables.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    # 列在该表中的位置（决定 prompt 里 schema 的呈现顺序）
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    column_name: Mapped[str] = mapped_column(String(64), nullable=False)
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 人类可读描述（含原表头，如「单价(元)」）—— 中文问法能对上英文标识符的关键
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 低基数列的取值清单（内联进 prompt，根治大小写/单复数写错）
    enum_values: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    is_nullable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )

    table: Mapped["AnalyticsTable"] = relationship(back_populates="columns")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AnalyticsColumn {self.column_name!r} {self.data_type}>"
