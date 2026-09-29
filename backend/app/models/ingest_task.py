"""入库任务状态模型（P4 的 ARQ worker 会写这张表；P3 先建表与仓储）。

上传即创建 queued 任务并返回 task_id，worker 领取后置 running→done/failed，
状态可轮询/SSE 观测。id 用 uuid（字符串主键），避免自增 id 泄漏吞吐信息。
"""

from __future__ import annotations

import uuid
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class TaskStatus(str, PyEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _uuid_hex() -> str:
    return uuid.uuid4().hex


class IngestTask(Base, TimestampMixin):
    """一次异步入库任务。"""

    __tablename__ = "ingest_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    document_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TaskStatus.QUEUED.value,
        server_default=TaskStatus.QUEUED.value, index=True,
    )
    # 0~100 进度百分比
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<IngestTask id={self.id!r} status={self.status!r} progress={self.progress}>"
