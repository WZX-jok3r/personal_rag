"""异步 Redis 客户端与会话热缓存。

- Redis 是「缓存 + 队列承载」，非事实来源：会话历史丢可从 PostgreSQL 重建。
- 会话历史用 Redis List 存（每个元素是一条 JSON 化的 {role,content}），
  RPUSH 追加、LTRIM 保留最近 N 条、EXPIRE 落 TTL，天然对齐 src 内存 SessionStore 的
  MAX_HISTORY_MESSAGES / SESSION_TTL_SECONDS 语义。
- 单例 `redis_client` 由 lifespan init()/dispose()；deps.get_redis 取用。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger(__name__)


class RedisClient:
    """Redis 异步连接的惰性单例。"""

    def __init__(self, url: Optional[str] = None) -> None:
        self._url = url or settings.redis_url
        self._client: Optional[Redis] = None

    @property
    def client(self) -> Redis:
        if self._client is None:
            self.init()
        assert self._client is not None
        return self._client

    def init(self) -> None:
        if self._client is not None:
            return
        self._client = Redis.from_url(self._url, decode_responses=True)
        logger.info("[Redis] 异步客户端已初始化")

    async def dispose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("[Redis] 连接已关闭")

    async def healthcheck(self) -> bool:
        return bool(await self.client.ping())


redis_client = RedisClient()


class SessionCache:
    """会话历史的热缓存（Redis List）。所有读写以 session_id 为键、带 TTL。"""

    def __init__(self, client: RedisClient = redis_client) -> None:
        self._rc = client

    @staticmethod
    def _key(session_id: str) -> str:
        return f"session:{session_id}:history"

    async def append(
        self, session_id: str, message: Dict[str, str], max_history: int, ttl: int,
    ) -> None:
        r = self._rc.client
        key = self._key(session_id)
        await r.rpush(key, json.dumps(message, ensure_ascii=False))
        # 只保留最近 max_history 条
        await r.ltrim(key, -max_history, -1)
        await r.expire(key, ttl)

    async def get(self, session_id: str) -> Optional[List[Dict[str, Any]]]:
        """命中返回历史列表（可能为空列表）；键不存在返回 None（表示需从 PG 重建）。"""
        r = self._rc.client
        raw = await r.lrange(self._key(session_id), 0, -1)
        if not raw:
            # 区分“空缓存”和“未缓存”：EXISTS=0 视为未命中，触发回源
            if await r.exists(self._key(session_id)) == 0:
                return None
            return []
        return [json.loads(item) for item in raw]

    async def reheat(
        self, session_id: str, history: List[Dict[str, Any]], max_history: int, ttl: int,
    ) -> None:
        """从 PG 读回历史后回填 Redis（覆盖式重建）。"""
        r = self._rc.client
        key = self._key(session_id)
        pipe = r.pipeline()
        pipe.delete(key)
        for msg in history:
            pipe.rpush(key, json.dumps(msg, ensure_ascii=False))
        pipe.ltrim(key, -max_history, -1)
        pipe.expire(key, ttl)
        await pipe.execute()

    async def delete(self, session_id: str) -> None:
        await self._rc.client.delete(self._key(session_id))

    async def touch(self, session_id: str, ttl: int) -> None:
        """刷新 TTL（读取即续期，等价于内存实现的 last_access 更新）。"""
        await self._rc.client.expire(self._key(session_id), ttl)
