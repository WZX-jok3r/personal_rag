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
        "status": "ok",
        "service": "rag-backend",
        "app_name": settings.app_name,
        "version": __version__,
        "environment": settings.environment,
        "auth_enabled": settings.auth_enabled,
        "time": datetime.now(timezone.utc).isoformat(),
    }
