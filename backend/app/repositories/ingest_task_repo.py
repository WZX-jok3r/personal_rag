"""入库任务仓储（P4 由 worker 写；P3 提供建/查/更新）。"""

from __future__ import annotations

from typing import Optional

from app.models.ingest_task import IngestTask, TaskStatus
from app.repositories.base import BaseRepository


class IngestTaskRepository(BaseRepository):
    async def create(
        self, tenant_id: Optional[str], document_id: Optional[int] = None,
    ) -> IngestTask:
        task = IngestTask(tenant_id=tenant_id, document_id=document_id,
                          status=TaskStatus.QUEUED.value)
        self._session.add(task)
        await self.flush()
        return task

    async def get(self, task_id: str) -> Optional[IngestTask]:
        return await self._session.get(IngestTask, task_id)

    async def update_status(
        self, task_id: str, status: str, progress: Optional[int] = None,
        error: Optional[str] = None, document_id: Optional[int] = None,
    ) -> Optional[IngestTask]:
        task = await self.get(task_id)
        if task is None:
            return None
        task.status = status
        if progress is not None:
            task.progress = progress
        if error is not None:
            task.error = error
        if document_id is not None:
            task.document_id = document_id
        await self.flush()
        return task
