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

    async def get(
        self, task_id: str, tenant_id: Optional[str] = None
    ) -> Optional[IngestTask]:
        """按 id 取任务；给了 tenant_id 时**同时校验归属**。

        ⚠️ 为什么要有租户校验（一次真实的 ACL 缺口）：
        任务状态接口原先只按 task_id 查，**任何已认证调用方都能查任意任务**，
        而返回体含 `document_id` 与 `error`（error 里可能带文件名/表名）。
        同一份代码里 `/documents` 列表与删除都做了租户校验，
        只有状态查询漏了 —— 这正是"两通道不对称"在仓储层的体现。

        归属策略与 document_repo 保持一致：
        - `tenant_id` 为 None（鉴权软关闭）→ 不校验，保持既有本地行为
        - 传了 tenant_id 但归属不同 → 视为**不存在**（返回 None → 404），
          而不是 403 —— 避免用状态码探测"某个 id 是否存在"
        """
        task = await self._session.get(IngestTask, task_id)
        if task is None:
            return None
        if tenant_id is not None and task.tenant_id != tenant_id:
            return None
        return task

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
