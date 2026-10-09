"""入库任务状态查询的租户归属校验（真连 PostgreSQL）。

## 本文件针对的真实 ACL 缺口

`GET /api/v1/documents/{task_id}/status` 原先只按 `task_id` 查，
**任何已认证调用方都能查任意任务**。返回体含 `document_id` 与 `error`，
而 `error` 里可能带文件名/表名等他人信息。

同一份代码里 `/documents` 列表与删除**都**做了租户校验，只有状态查询漏了 ——
这与 Agent 检索那次跨租户泄露**同源**：不是某一行写错，
而是"同类校验在不同接口上口径不一致"。

修复口径（与 document_repo 对齐，避免又出现第三种做法）：
  - `tenant_id is None`（鉴权软关闭）→ 不校验（保持既有本地行为）
  - `tenant_id` 与任务归属不符      → 返回 None ⇒ 404，
    而不是 403 —— 不用状态码泄露"该 id 是否存在"
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.ingest_task import IngestTask, TaskStatus
from app.repositories.ingest_task_repo import IngestTaskRepository

TENANT_A = "status-test-a"
TENANT_B = "status-test-b"
TASK_ID = "status_test_task_0001"


def _pg_available() -> bool:
    try:
        e = create_engine(settings.sync_postgres_url, connect_args={"connect_timeout": 5})
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        e.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="需要 PostgreSQL")


@pytest.fixture
def seeded():
    """插一条属于 TENANT_B 的任务。"""
    e = create_engine(settings.sync_postgres_url)
    with e.begin() as c:
        c.execute(text("DELETE FROM ingest_tasks WHERE id = :i"), {"i": TASK_ID})
        c.execute(text(
            "INSERT INTO ingest_tasks (id, tenant_id, status, progress) "
            "VALUES (:i, :t, :s, 0)"),
            {"i": TASK_ID, "t": TENANT_B, "s": TaskStatus.QUEUED.value})
    yield
    with e.begin() as c:
        c.execute(text("DELETE FROM ingest_tasks WHERE id = :i"), {"i": TASK_ID})
    e.dispose()


@pytest.fixture
async def session():
    engine = create_async_engine(settings.postgres_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


class TestIngestTaskTenantScoping:
    @pytest.mark.asyncio
    async def test_owner_can_read(self, seeded, session):
        task = await IngestTaskRepository(session).get(TASK_ID, TENANT_B)
        assert task is not None, "归属租户应能读到自己的任务"
        assert task.tenant_id == TENANT_B

    @pytest.mark.asyncio
    async def test_other_tenant_gets_nothing(self, seeded, session):
        """核心断言：别的租户查不到（返回 None ⇒ 上层转 404）。"""
        task = await IngestTaskRepository(session).get(TASK_ID, TENANT_A)
        assert task is None, "跨租户读到了他人任务 —— 状态查询存在越权"

    @pytest.mark.asyncio
    async def test_unknown_task_also_none(self, seeded, session):
        """不存在的 id 与无权限的 id 结果一致（不泄露存在性）。"""
        assert await IngestTaskRepository(session).get("no_such_task", TENANT_A) is None

    @pytest.mark.asyncio
    async def test_none_tenant_skips_check(self, seeded, session):
        """鉴权软关闭（tenant_id=None）时不校验 —— 保持既有本地/评测行为。

        这是**刻意的向后兼容**：鉴权没开时没有"身份"概念，
        强行校验会让本地调试与既有测试全挂。
        """
        task = await IngestTaskRepository(session).get(TASK_ID, None)
        assert task is not None

    @pytest.mark.asyncio
    async def test_default_argument_keeps_old_callers_working(self, seeded, session):
        """不传 tenant_id 时行为不变（默认参数向后兼容）。"""
        task = await IngestTaskRepository(session).get(TASK_ID)
        assert task is not None


class TestServiceAndApiPlumbTenant:
    """服务层与 API 层必须真的把租户传下去（防止"加了参数但没接线"）。"""

    def test_service_signature_accepts_tenant(self):
        import inspect

        from app.services.ingestion_service import IngestionService

        params = inspect.signature(IngestionService.get_status).parameters
        assert "tenant_id" in params, "get_status 又不接受 tenant_id 了"

    def test_api_passes_principal_tenant(self):
        """API 端点必须把 principal.tenant_id 传给服务层。"""
        import inspect

        from app.api.v1 import documents

        src = inspect.getsource(documents.get_ingest_status)
        assert "principal.tenant_id" in src, (
            "状态接口没有把 principal.tenant_id 传下去 —— 越权缺口会重现"
        )

    def test_repository_filters_by_tenant(self):
        """仓储的 get 必须真的比较 tenant_id（而不是收了参数却不用）。"""
        import inspect

        src = inspect.getsource(IngestTaskRepository.get)
        assert "task.tenant_id" in src and "tenant_id" in src
        assert "!= tenant_id" in src or "== tenant_id" in src, (
            "get 收了 tenant_id 却没有比较 —— 参数被忽略了"
        )
