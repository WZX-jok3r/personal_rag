"""文档登记表（入库的元数据事实来源，替代 processed_cache/cache.json 的角色）。

一个已入库文件对应一行：以 (source, file_hash) 定位，支持增量判断与删除清理。
ChunkMeta 明细仍存于 Qdrant（向量+payload），此处只登记文档级信息，避免双写大对象。
"""

from __future__ import annotations

from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DocumentStatus(str, PyEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class Document(Base, TimestampMixin):
    """知识库文档登记。"""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 文件名 / 相对路径（与 Qdrant payload 的 source 对齐）
    source: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DocumentStatus.PENDING.value,
        server_default=DocumentStatus.PENDING.value, index=True,
    )
    # 解析统计等轻量元信息（页数/行数/表格数等）
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Document id={self.id} source={self.source!r} status={self.status!r}>"
