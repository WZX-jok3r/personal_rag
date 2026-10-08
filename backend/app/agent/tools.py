"""tools.py - Agent 工具定义与执行。

三个工具（**别贪多**）。工具 `description` 里刻意写清"什么时候**不**该用"——
这是提升路由准确率最低成本的手段，比在 prompt 里堆规则有效得多。

    kb_search        : 知识库检索（**复用现有 Retriever，零新增检索逻辑**）
    sql_query        : 只读 SQL 统计查询（复用 P2/P3 的 guard + executor）
    list_data_tables : 列出可查数据表（模型不确定有哪些数据时先调它）

⚠️ 与既有 RAG 链路的唯一接触点就是 `kb_search`：它内部调 `Retriever.search`，
    返回与 pipeline 完全相同的 `(chunks, dropped)` 结构，
    因此既能复用全部检索能力（混合召回/精排/低分过滤/租户过滤），
    又能让上层补发标准 `meta` 帧（见 events.py 的说明）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.analytics import schema as schema_mod
from app.core.config import settings

logger = logging.getLogger(__name__)


# ==================== OpenAI 兼容的工具 schema ====================

TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "kb_search",
            "description": (
                "在内部文档知识库中检索相关段落，用于回答关于产品规格、政策条款、"
                "操作手册、FAQ、合同约定等**非结构化内容**的问题。"
                "不适用于需要统计计算的问题（那类问题请用 sql_query）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或问题"},
                    "top_k": {"type": "integer", "description": "返回条数，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sql_query",
            "description": (
                "对业务数据表执行只读 SELECT 查询，用于**精确统计**："
                "计数、求和、平均、最大/最小、分组对比、排名、占比、差值。"
                "适用于员工/销售/费用/成本等结构化数据。"
                "只允许单条 SELECT，系统会自动追加行数上限。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "一条完整的 PostgreSQL SELECT 语句，不要代码围栏，不要分号结尾",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "一句话说明这条查询要回答什么（用于审计）",
                    },
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_data_tables",
            "description": (
                "列出当前可查询的业务数据表及其字段含义。"
                "当你不确定有哪些数据可查、或不确定某张表是否包含所需字段时，先调用它。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


@dataclass
class ToolOutcome:
    """一次工具执行的结果（统一结构，供 Agent Loop 消费）。"""

    ok: bool
    name: str
    # 给 LLM 看的观察文本（进 prompt）
    observation: str = ""
    # 给前端看的一句话摘要
    summary: str = ""
    # 结构化产物（kb_search 的 chunks / sql 的结果集等）
    payload: Dict[str, Any] = field(default_factory=dict)


# ==================== 执行器 ====================

class AgentTools:
    """工具执行器。持有所需依赖，便于单测注入假实现。"""

    def __init__(
        self,
        retriever: Optional[Any] = None,
        text2sql_engine: Optional[Any] = None,
        session_factory: Optional[Any] = None,
    ) -> None:
        self._retriever = retriever
        self._t2s = text2sql_engine
        self._session_factory = session_factory

    # ---- 延迟初始化（避免无外部服务时构造即失败）----
    @property
    def retriever(self):
        if self._retriever is None:
            from app.rag.retriever import Retriever
            self._retriever = Retriever()
        return self._retriever

    @property
    def text2sql(self):
        if self._t2s is None:
            from app.analytics.text2sql import get_text2sql_engine
            self._t2s = get_text2sql_engine()
        return self._t2s

    def _session(self) -> Session:
        """打开一个 rag 库会话（读 schema 元数据用）。"""
        from sqlalchemy import create_engine
        if self._session_factory is not None:
            return self._session_factory()
        engine = create_engine(settings.sync_postgres_url)
        return Session(engine)

    # ---- 工具：知识库检索 ----
    def kb_search(self, query: str, top_k: Optional[int] = None,
                  filter_dict: Optional[Dict[str, Any]] = None) -> ToolOutcome:
        """复用现有 Retriever。返回结构与 pipeline 一致，便于上层补发 meta 帧。"""
        k = int(top_k) if top_k else settings.top_k
        try:
            chunks, dropped = self.retriever.search(query, top_k=k, filter_dict=filter_dict)
        except Exception as e:  # noqa: BLE001
            logger.error("[tools] kb_search 失败: %s", e)
            return ToolOutcome(
                ok=False, name="kb_search",
                observation=f"知识库检索失败：{str(e)[:200]}",
                summary="知识库检索失败",
            )

        if not chunks:
            return ToolOutcome(
                ok=True, name="kb_search",
                observation="知识库中未找到相关内容。",
                summary="未找到相关内容",
                payload={"chunks": [], "dropped": dropped, "retrieved_count": 0},
            )

        from app.rag.source_formatter import format_sources
        lines = []
        for i, c in enumerate(chunks, 1):
            src = (c.get("metadata") or {}).get("source", "未知来源")
            lines.append(f"[{i}] （来源：{src}）\n{c.get('text', '')}")
        observation = "\n\n".join(lines)

        return ToolOutcome(
            ok=True, name="kb_search",
            observation=observation,
            summary=f"检索到 {len(chunks)} 段相关内容" + (f"，已过滤 {dropped} 段低相关" if dropped else ""),
            payload={
                "chunks": chunks,
                "dropped": dropped,
                "retrieved_count": len(chunks),
                "sources": format_sources(chunks, with_preview=False),
            },
        )

    # ---- 工具：SQL 查询 ----
    def sql_query(self, sql: str, tenant_id: Optional[str] = None,
                  purpose: str = "") -> ToolOutcome:
        """执行模型给的 SQL。

        走与分析引擎相同的 guard + executor（四层防御完整生效）。
        注意：这里**只执行模型写好的 SQL**，不做"自然语言 -> SQL"的二次翻译
        （那是 text2sql 工具的职责；Agent 场景下模型已直接产出 SQL，
          避免多绕一次 LLM 调用）。
        """
        from app.analytics.executor import get_executor
        from app.analytics.guard import guard_sql

        g = guard_sql(sql, settings.sql_max_rows)
        if not g.ok:
            return ToolOutcome(
                ok=False, name="sql_query",
                observation=g.user_message or g.reason,
                summary=f"SQL 被安全网关拒绝：{g.reason}",
                payload={"rejected": True, "reason": g.reason},
            )

        qr = get_executor().execute(g.sql, tenant_id=tenant_id)
        if not qr.ok:
            return ToolOutcome(
                ok=False, name="sql_query",
                observation=qr.observation,
                summary="SQL 执行失败",
                payload={"error": qr.error},
            )

        return ToolOutcome(
            ok=True, name="sql_query",
            observation=qr.observation,
            summary=f"查询返回 {qr.row_count} 行（{qr.elapsed_ms}ms）"
                    + ("［已截断］" if qr.truncated else ""),
            payload={
                "sql": g.sql,
                "columns": qr.columns,
                "rows": qr.rows,
                "row_count": qr.row_count,
                "elapsed_ms": qr.elapsed_ms,
                "truncated": qr.truncated,
            },
        )

    # ---- 工具：列出可查数据表 ----
    def list_data_tables(self, tenant_id: Optional[str] = None) -> ToolOutcome:
        try:
            s = self._session()
            try:
                schemas = schema_mod.load_table_schemas(s, tenant_id=tenant_id)
            finally:
                s.close()
        except Exception as e:  # noqa: BLE001
            logger.error("[tools] list_data_tables 失败: %s", e)
            return ToolOutcome(
                ok=False, name="list_data_tables",
                observation=f"读取数据表清单失败：{str(e)[:200]}",
                summary="读取数据表清单失败",
            )

        if not schemas:
            return ToolOutcome(
                ok=True, name="list_data_tables",
                observation="当前没有可查询的业务数据表。",
                summary="无可查数据表",
                payload={"tables": []},
            )

        return ToolOutcome(
            ok=True, name="list_data_tables",
            observation=schema_mod.render_schema(schemas),
            summary=f"共 {len(schemas)} 张可查数据表：" + "、".join(s.table_name for s in schemas),
            payload={"tables": [s.table_name for s in schemas]},
        )

    # ---- 统一分发 ----
    def dispatch(self, name: str, args: Dict[str, Any],
                 tenant_id: Optional[str] = None) -> ToolOutcome:
        if name == "kb_search":
            return self.kb_search(
                query=str(args.get("query", "")),
                top_k=args.get("top_k"),
            )
        if name == "sql_query":
            return self.sql_query(
                sql=str(args.get("sql", "")),
                tenant_id=tenant_id,
                purpose=str(args.get("purpose", "")),
            )
        if name == "list_data_tables":
            return self.list_data_tables(tenant_id=tenant_id)
        return ToolOutcome(
            ok=False, name=name,
            observation=f"未知工具：{name}",
            summary=f"未知工具 {name}",
        )
