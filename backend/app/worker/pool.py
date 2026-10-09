"""ARQ 连接池（API 侧入队用）。

与 worker 共享同一 Redis；进程级单例，lifespan 启动时创建、关闭时释放。
"""

from __future__ import annotations

from typing import Optional

from arq import create_pool
from arq.connections import ArqRedis

from app.worker.settings import WorkerSettings

_pool: Optional[ArqRedis] = None


async def create_arq_pool() -> ArqRedis:
    """创建（或返回已存在的）ARQ 连接池。"""
    global _pool
    if _pool is None:
        _pool = await create_pool(WorkerSettings.redis_settings)
    return _pool


async def close_arq_pool() -> None:
    """释放连接池。"""
    global _pool
    if _pool is not None:
        try:
            await _pool.aclose()
        except AttributeError:  # 兼容旧版 redis-py（无 aclose）
            await _pool.close()
        _pool = None


def get_arq_pool() -> Optional[ArqRedis]:
    """返回当前连接池（lifespan 未初始化时为 None）。"""
    return _pool
