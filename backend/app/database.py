"""异步数据库层（SQLAlchemy 2.0 async + asyncpg）。

单例 `db` 由 lifespan 调 init()/dispose() 统一管理连接池；请求级会话通过
`get_db_session` 依赖产出（deps.py 里 re-export）。`create_all` 仅供开发/测试便利，
生产以 Alembic 迁移为准。
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.models.base import Base

logger = logging.getLogger(__name__)


class Database:
    """封装 async 引擎与 sessionmaker 的惰性生命周期。"""

    def __init__(self, url: Optional[str] = None, echo: Optional[bool] = None) -> None:
        self._url = url or settings.postgres_url
        self._echo = settings.debug if echo is None else echo
        self._engine: Optional[AsyncEngine] = None
        self._sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self.init()
        assert self._engine is not None
        return self._engine

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is None:
            self.init()
        assert self._sessionmaker is not None
        return self._sessionmaker

    def init(self) -> None:
        """创建引擎与 sessionmaker（幂等）。连接池参数取自 settings。"""
        if self._engine is not None:
            return
        self._engine = create_async_engine(
            self._url,
            echo=self._echo,
            pool_pre_ping=True,
            pool_size=settings.postgres_pool_size,
            max_overflow=settings.postgres_max_overflow,
        )
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, autoflush=False,
        )
        logger.info("[DB] async engine/sessionmaker 已初始化")

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None
            logger.info("[DB] 连接池已释放")

    async def healthcheck(self) -> bool:
        """探活：SELECT 1。失败上抛，供 /health 捕获。"""
        async with self.sessionmaker() as session:
            await session.execute(text("SELECT 1"))
        return True

    async def create_all(self) -> None:
        """直接按模型建表（仅开发/测试；生产走 alembic upgrade head）。"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


# 全局单例
db = Database()


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个 AsyncSession，退出自动关闭。"""
    async with db.sessionmaker() as session:
        yield session
