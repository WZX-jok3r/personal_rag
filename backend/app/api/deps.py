"""
deps.py - FastAPI 公共依赖

集中管理路由层复用的依赖项（身份、DB、Redis、各 service）。
P2 提供身份依赖；P3 起补充 get_db / get_session_service（PG+Redis 持久化）。
"""

from typing import AsyncIterator, Optional

from fastapi import Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis import SessionCache
from app.core.security import Principal, resolve_principal
from app.database import db
from app.services.session_service import SessionService


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
