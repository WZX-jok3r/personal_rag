"""ORM 模型聚合入口。

集中 re-export 所有模型，确保 Alembic 导入 `app.models` 时 Base.metadata 已注册全部表；
`database.create_all` 与 `alembic --autogenerate` 都依赖此处完成模型注册。
"""

from app.models.analytics import AnalyticsColumn, AnalyticsTable
from app.models.base import Base, TimestampMixin
from app.models.chat import ChatSession, Message
from app.models.document import Document, DocumentStatus
from app.models.ingest_task import IngestTask, TaskStatus
from app.models.llm_usage import LlmUsage
from app.models.sql_audit import SqlAudit
from app.models.tenant import Tenant

__all__ = [
    "Base",
    "TimestampMixin",
    "Tenant",
    "ChatSession",
    "Message",
    "Document",
    "DocumentStatus",
    "IngestTask",
    "TaskStatus",
    "AnalyticsTable",
    "AnalyticsColumn",
    "SqlAudit",
    "LlmUsage",
]
