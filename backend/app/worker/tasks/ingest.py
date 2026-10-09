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

from app.analytics.seed import should_sync_to_analytics
from app.core.config import settings
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

    # 4) 表格类文件：同步到 SQL 侧（RAG + Text2SQL 双通道）
    #
    # ⚠️ 补上一段曾被漏掉的实现：设计文档《text2sql-数据层设计.md》明确要求
    #    「复用现有异步入库链路，把『表格转表』做成一个 ARQ 任务」，
    #    但先前只交付了手动 CLI，worker 未接线 —— 后果是**通过 API 上传的 xlsx
    #    不会出现在 SQL 侧**：RAG 能检索到、SQL 查不到，两条通道静默不一致。
    #
    # 关键设计：本步骤**失败不影响主流程**（不 re-raise）。
    #   理由：向量化成功即 RAG 侧可用；SQL 侧同步失败只应降级为"该文件暂时查不了"，
    #   不该让整个入库任务失败并触发 ARQ 重试（重试会重复做一遍向量化，代价高）。
    #   失败信息写进 record / 日志，便于排查。
    analytics_sync: Dict[str, Any] = {"attempted": False}
    if settings.analytics_sync_on_ingest and should_sync_to_analytics(Path(file_path)):
        analytics_sync["attempted"] = True
        try:
            from app.analytics.seed import sync_analytics_tables

            results = await asyncio.to_thread(sync_analytics_tables, Path(file_path))
            analytics_sync["ok"] = True
            analytics_sync["tables"] = [
                {"table": r.table_name, "sheet": r.sheet_name,
                 "rows": r.row_count, "columns": r.column_count}
                for r in results
            ]
            logger.info("[IngestJob] 任务 %s 已同步 %d 张表到 SQL 侧: %s",
                        task_id, len(results),
                        ", ".join(r.table_name for r in results))
        except Exception as e:  # noqa: BLE001
            analytics_sync["ok"] = False
            analytics_sync["error"] = str(e)[:300]
            logger.warning(
                "[IngestJob] 任务 %s 的 SQL 侧同步失败（不影响 RAG 入库）: %s",
                task_id, str(e)[:200],
            )

    logger.info("[IngestJob] 任务 %s 完成: %s -> %s", task_id, Path(file_path).name, status)
    return {**record, "analytics_sync": analytics_sync}
