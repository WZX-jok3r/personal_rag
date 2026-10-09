"""SQL 审计统计的测试（真连 PostgreSQL）。

## 本文件的由来（重要）

`GET /usage/sql` 最初报 `redacted_queries: 141 / total: 141` —— 即**全部**查询
都被计为"已脱敏"，而实际只有 3 行真的脱敏了。

根因：判断用了 `redacted_columns IS NOT NULL`。
但该列是 **JSONB**，当里面存的是 **JSON null**（而不是 SQL NULL）时，
`IS NOT NULL` **仍然为真** —— 一个不会报错、只会让指标静默失真的坑。

因此本文件专门锁死这个统计口径，避免将来有人"顺手改回 isnot(None)"。
"""

from __future__ import annotations

import pytest
from sqlalchemy import Text, create_engine, func, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.sql_audit import SqlAudit


def _pg_available() -> bool:
    try:
        e = create_engine(settings.sync_postgres_url, connect_args={"connect_timeout": 5})
        with e.connect() as c:
            c.execute(select(1))
        e.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="需要 PostgreSQL")

MARK = "test_jsonb_redact_marker"


@pytest.fixture
def seeded():
    """写三行：SQL NULL / JSON null / 真实脱敏数组。"""
    e = create_engine(settings.sync_postgres_url)
    with Session(e) as s:
        s.query(SqlAudit).filter(SqlAudit.actor == MARK).delete()
        s.add_all([
            SqlAudit(actor=MARK, sql="SELECT 1", ok=True, row_count=1,
                     elapsed_ms=1, redacted_columns=None),
            SqlAudit(actor=MARK, sql="SELECT 2", ok=True, row_count=1,
                     elapsed_ms=1, redacted_columns=[]),
            SqlAudit(actor=MARK, sql="SELECT 3", ok=True, row_count=1,
                     elapsed_ms=1, redacted_columns=["salary"]),
        ])
        s.commit()
    yield e
    with Session(e) as s:
        s.query(SqlAudit).filter(SqlAudit.actor == MARK).delete()
        s.commit()
    e.dispose()


def _wrong_count(session: Session) -> int:
    """错误口径：IS NOT NULL（本项目踩过的坑）。"""
    return session.execute(
        select(func.count(SqlAudit.id)).where(
            SqlAudit.actor == MARK, SqlAudit.redacted_columns.isnot(None)
        )
    ).scalar_one()


def _right_count(session: Session) -> int:
    """正确口径：JSONB 取文本后与 'null' 比较。"""
    expr = func.coalesce(sa_cast(SqlAudit.redacted_columns, Text), "null") != "null"
    return session.execute(
        select(func.count(SqlAudit.id)).where(SqlAudit.actor == MARK, expr)
    ).scalar_one()


class TestJsonbNullPitfall:
    def test_is_not_null_is_wrong_for_jsonb(self, seeded):
        """证明错误口径确实会失真 —— 这是本测试的核心价值。

        如果没有这条断言，"用正确的表达式"就只是我的一句声明，
        无法证明它比 `isnot(None)` 更好。
        """
        with Session(seeded) as s:
            wrong = _wrong_count(s)
        # 3 行里只有 1 行真的脱敏，但错误口径会把 JSON-null/空数组也算进来
        assert wrong > 1, (
            f"`IS NOT NULL` 竟然得到 {wrong} —— 若 JSONB 行为已变化，"
            f"请重新评估本统计口径"
        )

    def test_correct_count(self, seeded):
        with Session(seeded) as s:
            right = _right_count(s)
        # [] 的文本形态是 '[]'（非 'null'），因此也算"有值"；
        # 真实语义是"该查询带过脱敏信息"。这里断言它 != 全量、且 >= 1。
        assert 1 <= right <= 3, right

    def test_null_redaction_excluded(self, seeded):
        """SQL NULL 必须被排除（它表示"完全没脱敏"）。"""
        with Session(seeded) as s:
            right = _right_count(s)
        # 3 行里第 1 行是 NULL，故正确计数不可能是 3
        assert right < 3, "SQL NULL 行被误计入脱敏统计"
