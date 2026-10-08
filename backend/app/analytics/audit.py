"""audit.py - SQL 审计写入（同步落 rag 库）。

被 executor 在每次 SQL 执行后调用。**审计失败绝不阻断主流程** ——
调用方（executor._audit）已吞掉异常，这里只负责写入。
"""

from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)

# 进程级懒建 engine（审计是高频写入，复用连接池）
_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings.sync_postgres_url, pool_pre_ping=True)
    return _engine


def record_sql_audit(
    sql: str,
    actor: str = "",
    tenant_id: Optional[str] = None,
    ok: bool = False,
    row_count: int = 0,
    elapsed_ms: int = 0,
    error: str = "",
    redacted_columns: Optional[List[str]] = None,
) -> None:
    """写一条审计记录。失败由调用方兜住（不影响 SQL 主流程）。"""
    from app.models.sql_audit import SqlAudit

    with Session(_get_engine()) as s:
        s.add(SqlAudit(
            actor=actor or None,
            tenant_id=tenant_id,
            # 截断超长 SQL，避免极端情况下撑爆存储
            sql=sql[:8000],
            ok=ok,
            row_count=row_count,
            elapsed_ms=elapsed_ms,
            redacted_columns=redacted_columns or None,
            error=(error[:2000] or None),
        ))
        s.commit()
