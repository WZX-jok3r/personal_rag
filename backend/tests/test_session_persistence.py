"""会话持久化集成测试（P3 门禁）。

直打真实 PostgreSQL + Redis（docker compose 已起）；任一后端不可用则整模块 skip，
保证无外部依赖的环境里 pytest 仍能通过。核心验证两条门禁语义：
1. 会话跨“重启”保留：清空热缓存后用新 SessionService 实例仍能从 PG 回源历史。
2. 多租户 ACL 隔离：非本租户对同一 session 不可见 / 不可写 / 不可删。
"""

from __future__ import annotations

import pytest

from app.cache.redis import redis_client
from app.database import db
from app.services.session_service import SessionService

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _ensure_backends():
    try:
        await db.healthcheck()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 未就绪，跳过持久化集成测试: {e}")
    await db.create_all()  # 幂等；即便未跑 alembic 也保证表存在
    redis_client.init()
    try:
        await redis_client.healthcheck()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Redis 未就绪，跳过持久化集成测试: {e}")
    yield
    # pytest-asyncio 默认按函数新建事件循环；测试后释放连接池，
    # 避免 asyncpg 连接跨循环复用报 attached to a different loop。
    await db.dispose()
    await redis_client.dispose()


async def test_session_persists_across_restart():
    svc = SessionService(db.sessionmaker)
    tenant = "t_restart"
    sid = await svc.create(tenant)
    await svc.append(sid, tenant, {"role": "user", "content": "你好"})
    await svc.append(sid, tenant, {"role": "assistant", "content": "在的"})

    # 清空热缓存，模拟“进程重启后 Redis 为空”
    await svc._cache.delete(sid)

    # 全新服务实例（模拟重启），历史应从 PG 回源重建
    svc2 = SessionService(db.sessionmaker)
    history = await svc2.get_history(sid, tenant)
    assert history == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "在的"},
    ]

    await svc2.delete(sid, tenant)


async def test_multi_tenant_acl_isolation():
    svc = SessionService(db.sessionmaker)
    sid = await svc.create("tA")
    await svc.append(sid, "tA", {"role": "user", "content": "A的秘密"})

    # 其他租户不可见
    assert await svc.exists(sid, "tB") is False
    assert await svc.get_history(sid, "tB") == []

    # 越权写入应被静默忽略
    await svc.append(sid, "tB", {"role": "user", "content": "越权注入"})
    history_a = await svc.get_history(sid, "tA")
    assert history_a == [{"role": "user", "content": "A的秘密"}]

    # 越权删除失败，本租户删除成功
    assert await svc.delete(sid, "tB") is False
    assert await svc.delete(sid, "tA") is True
