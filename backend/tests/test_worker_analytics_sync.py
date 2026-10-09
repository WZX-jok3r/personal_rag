"""worker 与 SQL 通道（analytics）的接线测试。

## 为什么需要这个文件

设计文档《text2sql-数据层设计.md》要求「复用异步入库链路，把『表格转表』
做成一个 ARQ 任务」，但先前只交付了手动 CLI，**worker 从未接线** ——
后果是：通过 API 上传的 xlsx 只进 Qdrant、不进 SQL，
**RAG 能检索到、SQL 查不到**，两条通道静默不一致。

这类"文档说了但实现没做"的缺口不会报错，只会让功能少一半，
所以必须用测试把接线本身钉住。

本文件用假 sessionmaker + monkeypatch 掉真实 ETL，
只验证**接线与控制流**（是否调用、失败是否吞掉、非表格是否跳过），
不真连数据库（真链路由 tests/test_sql_executor_integration.py 覆盖）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.models.document import DocumentStatus
from app.models.ingest_task import TaskStatus
from app.worker.tasks.ingest import ingest_document


# ==================== 测试替身 ====================

class FakeRepo:
    """极简仓储替身，记录调用。"""

    calls: List[Dict[str, Any]] = []

    def __init__(self, session) -> None:
        pass

    async def update_status(self, task_id, status, progress=None, error=None) -> None:
        FakeRepo.calls.append({"repo": "task", "task_id": task_id,
                               "status": status, "progress": progress, "error": error})

    async def upsert(self, **kwargs) -> None:
        FakeRepo.calls.append({"repo": "doc", **kwargs})


class FakeSession:
    async def commit(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSessionMaker:
    """可被 `async with sessionmaker()` 使用的替身。"""

    def __call__(self):
        return FakeSession()


@pytest.fixture(autouse=True)
def _patch_repos(monkeypatch):
    FakeRepo.calls = []
    monkeypatch.setattr("app.worker.tasks.ingest.IngestTaskRepository", FakeRepo)
    monkeypatch.setattr("app.worker.tasks.ingest.DocumentRepository", FakeRepo)
    yield
    FakeRepo.calls = []


def _ctx(process_file) -> Dict[str, Any]:
    return {"db": FakeSessionMaker(), "process_file": process_file,
            "vector_store": object()}


def _ok_record(fmt: str = "xlsx") -> Dict[str, Any]:
    return {"status": "success", "format": fmt, "file_hash": "h",
            "chunk_count": 3}


# ==================== 接线验证 ====================

class TestAnalyticsSyncIsWired:
    """核心：worker 必须真的调用 SQL 侧同步。"""

    @pytest.mark.asyncio
    async def test_xlsx_triggers_analytics_sync(self, monkeypatch, tmp_path):
        calls: List[Path] = []

        def fake_sync(path: Path):
            calls.append(path)

            class R:
                table_name = "employees"
                sheet_name = "Employees"
                row_count = 100
                column_count = 7

            return [R()]

        monkeypatch.setattr("app.analytics.seed.sync_analytics_tables", fake_sync)

        f = tmp_path / "data.xlsx"
        f.write_bytes(b"x")
        out = await ingest_document(_ctx(lambda *a, **k: _ok_record()),
                                    str(f), "t1", None)

        assert len(calls) == 1, "xlsx 必须触发 SQL 侧同步"
        assert out["analytics_sync"]["attempted"] is True
        assert out["analytics_sync"]["ok"] is True
        assert out["analytics_sync"]["tables"] == [
            {"table": "employees", "sheet": "Employees", "rows": 100, "columns": 7}
        ]

    @pytest.mark.asyncio
    async def test_pdf_does_not_trigger_analytics_sync(self, monkeypatch, tmp_path):
        """非表格文件不应触发 —— PDF 表格提取是启发式的，建表会引入静默错误。"""
        calls: List[Path] = []
        monkeypatch.setattr("app.analytics.seed.sync_analytics_tables",
                            lambda p: calls.append(p) or [])

        f = tmp_path / "doc.pdf"
        f.write_bytes(b"x")
        out = await ingest_document(_ctx(lambda *a, **k: _ok_record("pdf")),
                                    str(f), "t2", None)

        assert calls == [], "pdf 不应触发 SQL 侧同步"
        assert out["analytics_sync"]["attempted"] is False

    @pytest.mark.asyncio
    async def test_disabled_by_config(self, monkeypatch, tmp_path):
        """开关关闭时只走 RAG（便于对照与排障）。"""
        from app.core.config import settings

        calls: List[Path] = []
        monkeypatch.setattr("app.analytics.seed.sync_analytics_tables",
                            lambda p: calls.append(p) or [])
        monkeypatch.setattr(settings, "analytics_sync_on_ingest", False)

        f = tmp_path / "data.xlsx"
        f.write_bytes(b"x")
        out = await ingest_document(_ctx(lambda *a, **k: _ok_record()),
                                    str(f), "t3", None)

        assert calls == []
        assert out["analytics_sync"]["attempted"] is False


# ==================== 失败隔离（关键设计）====================

class TestAnalyticsFailureIsIsolated:
    """SQL 侧同步失败**不能**让整个入库任务失败。

    理由：向量化成功即 RAG 侧可用。SQL 侧失败只应降级为"该文件暂时查不了"；
    若让它 re-raise，ARQ 会重试整个任务 —— 而重试会重复做一遍昂贵的向量化。
    """

    @pytest.mark.asyncio
    async def test_sync_failure_does_not_fail_task(self, monkeypatch, tmp_path):
        def boom(path):
            raise RuntimeError("analytics db down")

        monkeypatch.setattr("app.analytics.seed.sync_analytics_tables", boom)

        f = tmp_path / "data.xlsx"
        f.write_bytes(b"x")
        # 不应抛异常
        out = await ingest_document(_ctx(lambda *a, **k: _ok_record()),
                                    str(f), "t4", None)

        assert out["analytics_sync"]["attempted"] is True
        assert out["analytics_sync"]["ok"] is False
        assert "analytics db down" in out["analytics_sync"]["error"]

        # 关键：任务仍被标记为 DONE（不是 FAILED）
        statuses = [c["status"] for c in FakeRepo.calls if c["repo"] == "task"]
        assert TaskStatus.DONE.value in statuses
        assert TaskStatus.FAILED.value not in statuses, (
            "SQL 侧失败不应把入库任务标记为 FAILED（会触发无谓的整任务重试）"
        )

    @pytest.mark.asyncio
    async def test_vectorization_failure_still_fails_task(self, monkeypatch, tmp_path):
        """对照组：向量化失败**必须**让任务失败并 re-raise（触发 ARQ 重试）。"""
        def boom(*a, **k):
            raise RuntimeError("embedding down")

        f = tmp_path / "data.xlsx"
        f.write_bytes(b"x")
        with pytest.raises(RuntimeError):
            await ingest_document(_ctx(boom), str(f), "t5", None)

        statuses = [c["status"] for c in FakeRepo.calls if c["repo"] == "task"]
        assert TaskStatus.FAILED.value in statuses


class TestReturnValueShape:
    @pytest.mark.asyncio
    async def test_record_fields_preserved(self, monkeypatch, tmp_path):
        """原有返回字段必须保留（不能因为加了 analytics_sync 就丢掉它们）。"""
        monkeypatch.setattr("app.analytics.seed.sync_analytics_tables", lambda p: [])
        f = tmp_path / "data.xlsx"
        f.write_bytes(b"x")
        rec = _ok_record()
        out = await ingest_document(_ctx(lambda *a, **k: rec), str(f), "t6", None)

        for k in ("status", "format", "file_hash", "chunk_count"):
            assert out[k] == rec[k], f"字段 {k} 丢失"
        assert "analytics_sync" in out
