"""单轮问答路由：POST /api/v1/query。

pipeline 内部是阻塞的 requests 调用，放到线程池执行（run_in_threadpool），
不阻塞事件循环；鉴权开启时按当前租户强制过滤检索范围（迁移自 src/api.py）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.api.deps import enforced_filter, get_pipeline, get_principal, resolve_top_k
from app.core.security import Principal
from app.rag.pipeline import RAGPipeline
from app.schemas.chat import QueryRequest, QueryResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["query"])


@router.post("/query", response_model=QueryResponse, summary="单轮问答")
async def single_query(
    req: QueryRequest,
    principal: Principal = Depends(get_principal),
    pipe: RAGPipeline = Depends(get_pipeline),
) -> QueryResponse:
    result = await run_in_threadpool(
        pipe.query,
        req.query,
        top_k=resolve_top_k(req.top_k),
        filter_dict=enforced_filter(req.filter_dict, principal),
    )
    return QueryResponse(**result)
