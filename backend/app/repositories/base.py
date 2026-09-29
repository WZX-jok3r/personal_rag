"""仓储基类：持有请求级 AsyncSession，只做 flush，不擅自 commit。

事务边界由上层 service 掌控（如「建会话 + 落首条消息」需原子提交），仓储方法保持
幂等、可组合。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def flush(self) -> None:
        await self._session.flush()
