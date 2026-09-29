"""
健康检查（api/v1/health）

GET /api/v1/health  -> 存活探针（liveness），不依赖外部服务，永远快速返回。
后续阶段（P3）再补 readiness：探测 PostgreSQL / Redis / Qdrant 连通性。
"""

from datetime import datetime, timezone

from fastapi import APIRouter

from app import __version__
from app.core.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {
        # 存活探针（liveness）：不依赖外部服务，永远快速返回
        "status": "ok",
        "service": "rag-backend",
        "app_name": settings.app_name,
        "version": __version__,
        "environment": settings.environment,
        "auth_enabled": settings.auth_enabled,
        "time": datetime.now(timezone.utc).isoformat(),
        # 配置回显（与前端 HealthInfo / src/api.py 对齐）
        "embedding_model": settings.embedding_model,
        "llm_model": settings.llm_model,
        "collection": settings.qdrant_collection_name,
        "retrieval_mode": settings.retrieval_mode,
        "rerank_enabled": settings.rerank_enabled,
        "rerank_model": settings.rerank_model if settings.rerank_enabled else None,
        "rerank_candidates": settings.rerank_candidates if settings.rerank_enabled else None,
        "default_top_k": settings.top_k,
        "tenant_field": settings.tenant_field,
    }
