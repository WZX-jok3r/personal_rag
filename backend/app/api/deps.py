"""
deps.py - FastAPI 公共依赖

集中管理路由层复用的依赖项（身份、DB、Redis、各 service）。
P2 提供身份依赖；P3 起补充 get_db / get_session_service（PG+Redis 持久化）。
"""

from typing import AsyncIterator, Optional

from fastapi import Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis import SessionCache
from app.core.config import settings
from app.core.security import Principal, resolve_principal
from app.database import db
from app.rag.pipeline import RAGPipeline
from app.services.ingestion_service import IngestionService
from app.services.session_service import SessionService
from app.worker.pool import get_arq_pool


def get_principal(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None),
) -> Principal:
    """FastAPI 依赖：从请求头解析调用方身份（详见 core.security.resolve_principal）。"""
    return resolve_principal(x_api_key, authorization)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个 AsyncSession（自动关闭；提交由 service 边界掌控）。"""
    async with db.sessionmaker() as session:
        yield session


# 会话服务为无状态持有者（内部每次操作开短会话），进程级复用一份即可
_session_service: Optional[SessionService] = None


def get_session_service() -> SessionService:
    """FastAPI 依赖：返回进程级单例 SessionService（PG 事实来源 + Redis 热缓存）。"""
    global _session_service
    if _session_service is None:
        _session_service = SessionService(db.sessionmaker, SessionCache())
    return _session_service


def get_ingestion_service() -> IngestionService:
    """FastAPI 依赖：入库编排服务（建记录 + 经 ARQ 连接池入队）。

    ARQ 连接池在 lifespan 启动时创建；无池（未起 infra）时入队会失败，
    但与「下载/查询状态」解耦，状态真相始终在 PG。
    """
    return IngestionService(db.sessionmaker, get_arq_pool())


# RAGPipeline 为进程级单例：避免每请求重建 Qdrant / LLM 连接
_pipeline: Optional[RAGPipeline] = None


def get_pipeline() -> RAGPipeline:
    """FastAPI 依赖：返回进程级单例 RAGPipeline（懒初始化）。"""
    global _pipeline
    if _pipeline is None:
        _pipeline = RAGPipeline()
    return _pipeline


def resolve_top_k(top_k: Optional[int]) -> int:
    """请求未指定 top_k 时回退到配置默认值。"""
    return int(top_k) if top_k else settings.top_k


def enforced_filter(
    client_filter: Optional[dict], principal: Principal
) -> Optional[dict]:
    """服务端强制叠加租户隔离（迁移自 src/api.py）。

    - 客户端可传普通业务过滤（如 format），但绝不能覆盖租户键；
    - tenant_id 以已认证 Principal 为准（后写覆盖）；
    - 鉴权关闭时 principal.tenant_id 为 None，不注入租户条件。
    """
    merged = dict(client_filter or {})
    merged.pop(settings.tenant_field, None)
    if principal.tenant_id is not None:
        merged[settings.tenant_field] = principal.tenant_id
    return merged or None
