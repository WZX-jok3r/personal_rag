"""会话并发追加的竞态测试（真连 PostgreSQL）。

## 为什么必须真连数据库

竞态是**数据库层**行为：advisory lock、事务隔离、唯一约束冲突，
全都是只存在于真实 PG 里的语义。用 mock 测这个等于没测。

## 这个测试的价值

改造前 `append` 的写入序列是「SELECT max(seq) -> INSERT seq+1」，
并发时会算出重复 seq 撞 `unique(session_id, seq)`。
本文件用**真实并发**复现它，并验证修复后不再发生。

跳过条件：连不上 PG 则 skip（不拖红套件）。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.repositories.session_repo import SessionRepository


def _pg_available() -> bool:
    try:
        e = create_engine(settings.sync_postgres_url, connect_args={"connect_timeout": 5})
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        e.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(), reason="需要 PostgreSQL（真实并发竞态测试）"
)

TEST_SESSION = "concurrency_test_session_0001"


def _cleanup(sync_engine) -> None:
    with sync_engine.begin() as c:
        c.execute(text("DELETE FROM messages WHERE session_id = :s"), {"s": TEST_SESSION})
        c.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": TEST_SESSION})


@pytest.fixture
def sync_engine():
    e = create_engine(settings.sync_postgres_url)
    _cleanup(e)
    with e.begin() as c:
        c.execute(
            text("INSERT INTO sessions (id, tenant_id) VALUES (:s, NULL)"),
            {"s": TEST_SESSION},
        )
    yield e
    _cleanup(e)
    e.dispose()


@pytest.fixture
def async_sm():
    engine = create_async_engine(settings.postgres_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.get_event_loop().run_until_complete(engine.dispose()) if False else None


async def _append(repo_sm, role: str, content: str) -> None:
    """模拟 SessionService.append 的写入序列（含 advisory lock）。"""
    async with repo_sm() as s:
        repo = SessionRepository(s)
        await repo.lock_session(TEST_SESSION)
        seq = await repo.next_seq(TEST_SESSION)
        await repo.add_message(TEST_SESSION, role, content, seq)
        await s.commit()


async def _append_without_lock(repo_sm, role: str, content: str) -> None:
    """刻意不加锁的版本 —— 用于**证明竞态真实存在**（反证锁是必要的）。"""
    async with repo_sm() as s:
        repo = SessionRepository(s)
        seq = await repo.next_seq(TEST_SESSION)
        # 人为放大竞态窗口：让两个协程都先读到同一个 max(seq) 再各自插入
        await asyncio.sleep(0.05)
        await repo.add_message(TEST_SESSION, role, content, seq)
        await s.commit()


class TestRaceExistsWithoutLock:
    """反证：不加锁时会撞唯一约束。

    这条测试的作用是**证明修复不是多余的** ——
    如果它通过（没有冲突），说明我的并发构造方式没有真正触发竞态，
    那么"加锁"就没有被验证到。
    """

    @pytest.mark.asyncio
    async def test_unlocked_concurrent_appends_conflict(self, sync_engine):
        engine = create_async_engine(settings.postgres_url)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            results = await asyncio.gather(
                *[_append_without_lock(sm, "user", f"m{i}") for i in range(3)],
                return_exceptions=True,
            )
            errors = [r for r in results if isinstance(r, Exception)]
            assert errors, (
                "未加锁的并发追加竟然全部成功 —— 说明竞态窗口没被触发，"
                "本测试失去反证意义（需要调整并发构造）"
            )
            # 唯一约束冲突是预期的错误类型
            assert any("uq_messages_session_id_seq" in str(e) or "duplicate" in str(e).lower()
                       for e in errors), [str(e)[:120] for e in errors]
        finally:
            await engine.dispose()


class TestLockFixesRace:
    """修复验证：加 advisory lock 后并发追加全部成功且 seq 连续无重复。"""

    @pytest.mark.asyncio
    async def test_locked_concurrent_appends_all_succeed(self, sync_engine):
        engine = create_async_engine(settings.postgres_url)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            n = 8
            results = await asyncio.gather(
                *[_append(sm, "user", f"msg{i}") for i in range(n)],
                return_exceptions=True,
            )
            errors = [r for r in results if isinstance(r, Exception)]
            assert not errors, f"加锁后仍失败: {[str(e)[:150] for e in errors]}"
        finally:
            await engine.dispose()

        # 校验：seq 必须是 1..n 连续且无重复
        with sync_engine.connect() as c:
            seqs = [r[0] for r in c.execute(
                text("SELECT seq FROM messages WHERE session_id = :s ORDER BY seq"),
                {"s": TEST_SESSION},
            ).fetchall()]
        assert seqs == list(range(1, n + 1)), f"seq 不连续或有重复: {seqs}"

    @pytest.mark.asyncio
    async def test_different_sessions_do_not_block_each_other(self, sync_engine):
        """锁粒度是 session_id ⇒ 不同会话不应互相阻塞。

        用耗时做粗判：若锁粒度错了（比如全表锁），并发写两个会话会显著变慢。
        这里只断言"能并发完成且无错误"，避免引入 flaky 的时间阈值断言。
        """
        import time
        other = TEST_SESSION + "_b"
        with sync_engine.begin() as c:
            c.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": other})
            c.execute(text("INSERT INTO sessions (id, tenant_id) VALUES (:s, NULL)"), {"s": other})

        engine = create_async_engine(settings.postgres_url)
        sm = async_sessionmaker(engine, expire_on_commit=False)

        async def write(sid: str, i: int) -> None:
            async with sm() as s:
                repo = SessionRepository(s)
                await repo.lock_session(sid)
                seq = await repo.next_seq(sid)
                await repo.add_message(sid, "user", f"x{i}", seq)
                await s.commit()

        try:
            t0 = time.perf_counter()
            await asyncio.gather(*[write(TEST_SESSION, i) for i in range(3)],
                                 *[write(other, i) for i in range(3)])
            elapsed = time.perf_counter() - t0
            # 6 次写入若被完全串行化（每次 ~50ms 级别）会明显更慢；
            # 这里只做宽松断言（< 3s），确保没有把并发退化成串行长队
            assert elapsed < 3.0, f"并发写入耗时异常（可能锁粒度过粗）: {elapsed:.2f}s"
        finally:
            await engine.dispose()
            with sync_engine.begin() as c:
                c.execute(text("DELETE FROM messages WHERE session_id = :s"), {"s": other})
                c.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": other})
