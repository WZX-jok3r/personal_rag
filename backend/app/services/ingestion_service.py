"""入库编排服务：接收文件→落 PG 任务/文档记录→入队 ARQ→立即返回 task_id。

设计要点：
- submit 只做「建记录 + 入队」，解析/向量化等重活交给 worker 后台执行，故大文件也能秒返回。
- 状态真相在 PG（IngestTask/Document），Redis 仅作队列；客户端凭 task_id 轮询 get_status。
- arq 连接池经依赖注入，便于用假池单测（无需真起 worker）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import NotFoundError
from app.ingestion.loader import compute_file_hash
from app.models.document import DocumentStatus
from app.repositories.document_repo import DocumentRepository
from app.repositories.ingest_task_repo import IngestTaskRepository

logger = logging.getLogger(__name__)

# ARQ 任务名（与 worker/tasks/ingest.py 的函数名一致）
INGEST_JOB = "ingest_document"


class IngestionService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], arq_pool: Any) -> None:
        self._sm = sessionmaker
        self._pool = arq_pool

    def queue_ready(self) -> bool:
        """ARQ 入队连接池是否就绪（未起 infra 时为 None）。"""
        return self._pool is not None

    async def submit(
        self,
        file_path: str | Path,
        tenant_id: Optional[str] = None,
        tenant_ids: Optional[List[str]] = None,
    ) -> str:
        """建 Document(pending)+IngestTask(queued)，入队并立即返回 task_id。"""
        path = Path(file_path)
        # 计算 hash 属文件 I/O，放线程池避免阻塞事件循环（大文件也很快，只读不解析）
        file_hash = await asyncio.to_thread(compute_file_hash, path)
        fmt = path.suffix.lower().lstrip(".")

        async with self._sm() as s:
            doc, _created = await DocumentRepository(s).upsert(
                source=path.name,
                file_hash=file_hash,
                fmt=fmt,
                tenant_id=tenant_id,
                status=DocumentStatus.PENDING.value,
            )
            task = await IngestTaskRepository(s).create(
                tenant_id=tenant_id, document_id=doc.id
            )
            await s.commit()
            task_id = task.id

        # 入队（_job_start 默认立即可被 worker 领取）
        await self._pool.enqueue_job(
            INGEST_JOB, str(path), task_id, tenant_ids or ([tenant_id] if tenant_id else None)
        )
        logger.info("[Ingest] 已入队 task_id=%s file=%s", task_id, path.name)
        return task_id

    async def get_status(self, task_id: str) -> Dict[str, Any]:
        """查询任务状态（供前端轮询进度）。不存在抛 NotFoundError。"""
        async with self._sm() as s:
            task = await IngestTaskRepository(s).get(task_id)
            if task is None:
                raise NotFoundError(f"ingest task {task_id} not found")
            return {
                "task_id": task.id,
                "document_id": task.document_id,
                "status": task.status,
                "progress": task.progress,
                "error": task.error,
            }
