"""异步入库任务（ARQ job）：处理单个文件的解析→切分→向量化→索引，并维护任务状态。

状态机：queued（提交时建）→ running（本任务开始）→ done / failed。
失败时把状态置 failed 且记录 error，并重新抛出异常以触发 ARQ 自动重试（max_tries）。

可测性：process_file 与 vector_store 均可经 ctx 注入，单测无需真连 Qdrant / SiliconFlow。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.ingestion.indexer import process_file
from app.models.document import DocumentStatus
from app.models.ingest_task import TaskStatus
from app.repositories.document_repo import DocumentRepository
from app.repositories.ingest_task_repo import IngestTaskRepository
from app.vector.qdrant import get_vector_store

logger = logging.getLogger(__name__)


async def ingest_document(
    ctx: Dict[str, Any],
    file_path: str,
    task_id: str,
    tenant_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """ARQ 任务函数：入库一个文件。ctx 由 worker on_startup 注入（db / vector_store）。"""
    sessionmaker = ctx["db"]
    # 默认走真实实现；测试可注入 fake
    process = ctx.get("process_file", process_file)
    vector_store = ctx.get("vector_store")

    # 1) 置 running
    async with sessionmaker() as s:
        await IngestTaskRepository(s).update_status(task_id, TaskStatus.RUNNING.value, progress=10)
        await s.commit()

    # 2) 执行重活（同步：解析/切分/embedding/upsert）放到线程池，避免阻塞事件循环
    try:
        if vector_store is None:
            vector_store = await asyncio.to_thread(get_vector_store)
        record = await asyncio.to_thread(process, Path(file_path), vector_store, tenant_ids)
    except Exception as e:  # noqa: BLE001
        async with sessionmaker() as s:
            await IngestTaskRepository(s).update_status(task_id, TaskStatus.FAILED.value, error=str(e))
            await s.commit()
        logger.error("[IngestJob] 任务 %s 处理 %s 失败: %s", task_id, file_path, e)
        raise  # 重新抛出，交由 ARQ 按 max_tries 重试

    # 3) 成功：登记 Document 终态 + 任务 done
    status = record.get("status")
    doc_status = DocumentStatus.READY.value if status == "success" else DocumentStatus.PENDING.value
    fmt = record.get("format") or Path(file_path).suffix.lower().lstrip(".")
    async with sessionmaker() as s:
        await DocumentRepository(s).upsert(
            source=Path(file_path).name,
            file_hash=record.get("file_hash", ""),
            fmt=fmt,
            tenant_id=(tenant_ids[0] if tenant_ids else None),
            chunk_count=record.get("chunk_count", 0),
            status=doc_status,
            meta=record,
        )
        await IngestTaskRepository(s).update_status(task_id, TaskStatus.DONE.value, progress=100)
        await s.commit()

    logger.info("[IngestJob] 任务 %s 完成: %s -> %s", task_id, Path(file_path).name, status)
    return record
