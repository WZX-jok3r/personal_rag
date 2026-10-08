"""loop.py - 有界状态机 Agent Loop（手写，不引入框架）。

## 为什么手写（改造方案 4.5 / 5.4）
只有 3 个工具、循环上界明确、无多 Agent 协作、无人在回路审批；
且必须复用既有 SSE 契约与 hermetic 测试体系（45 例单测不真连外部服务）。
引入图编排框架 = 用 1000 行抽象解决 300 行问题，并让调试链路变长。
切换边界：多 Agent 协作 / HITL 审批 -> LangGraph；复杂结构化输出 -> Pydantic AI。

## 五重防死循环（面试必问，也是生产必须）
    1. MAX_STEPS = 5          步数硬上界（**状态机而非 while True**）
    2. WALL_CLOCK = 60s       墙钟超时（防单步慢）
    3. MAX_LLM_CALLS = 8      **成本上界**（框架默认往往没有，但生产必须有）
    4. 相同工具+相同参数去重   连续两次相同调用判为循环，立即跳出
    5. 任一步失败 -> **降级到纯 RAG**，而不是把错误抛给用户（保证可用性不倒退）

## 降级必须"原样走 RAG"
降级时若只是"调 kb_search 然后生成答案"，前端拿不到标准 `meta` 帧，
来源列表与「已隐藏 N 条」徽标会**永久空着**（见 events.py 说明）。
因此 `kb_search` 一旦执行，本模块**必定补发 meta 帧**。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from app.agent import events as ev
from app.agent.router import Route, RouteDecision, route_by_rules
from app.agent.tools import TOOLS, AgentTools, ToolOutcome
from app.core.config import settings

logger = logging.getLogger(__name__)

# ---- 上界（五重保护的前三重）----
MAX_STEPS = 5
WALL_CLOCK_SECONDS = 60.0
MAX_LLM_CALLS = 8

SYSTEM_PROMPT = """你是一个企业内部知识库助手，可以调用工具来回答用户问题。

你有以下工具：
- kb_search：在内部文档知识库中检索（产品规格、政策条款、手册、FAQ、合同约定等非结构化内容）
- sql_query：对业务数据表执行只读 SELECT 做精确统计（计数、求和、平均、最大最小、分组对比、排名、差值）
- list_data_tables：列出可查询的数据表及字段含义（不确定有哪些数据时先调它）

【决策原则】
1. 政策/条款/规格/操作类问题 → 用 kb_search。
2. 需要精确计算的统计类问题 → 用 sql_query。**不要用检索到的片段做算术**，
   因为检索只能拿到局部数据，聚合结果必须由数据库计算。
3. 问题同时需要文档依据与数据计算时（例如"研发平均薪资是否超过文档规定的带宽上限"），
   **两个工具都要调用**，再做联合判断。
4. 优先一次调用解决问题，不要反复试探。
5. 回答必须有依据：数据类回答要给出具体数字；文档类回答要基于检索到的内容。
   如果工具没有返回有用信息，就直说没有找到，**不要编造**。
"""


@dataclass
class AgentState:
    """Agent 运行状态（显式状态机，便于断言与调试）。"""

    question: str
    history: List[Dict[str, str]] = field(default_factory=list)
    steps: int = 0
    llm_calls: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    seen_calls: set = field(default_factory=set)   # 去重键
    final_answer: Optional[str] = None
    degraded: bool = False
    degraded_reason: str = ""
    route: Optional[RouteDecision] = None


class AgentLoop:
    """有界 Agent 状态机。"""

    def __init__(
        self,
        tools: Optional[AgentTools] = None,
        llm: Optional[Any] = None,
        max_steps: int = MAX_STEPS,
        wall_clock: float = WALL_CLOCK_SECONDS,
        max_llm_calls: int = MAX_LLM_CALLS,
    ) -> None:
        self.tools = tools or AgentTools()
        self._llm = llm
        self.max_steps = max_steps
        self.wall_clock = wall_clock
        self.max_llm_calls = max_llm_calls
        # 可观测计数（P7 的成本记账会读它）
        self.last_stats: Dict[str, Any] = {}
        # 跨方法传递的临时状态（显式初始化，避免 getattr 兜底掩盖拼写错误）
        self._last_outcome: Optional[ToolOutcome] = None
        self._forced_observation: Optional[ToolOutcome] = None
        self._pending_meta: Optional[Dict[str, Any]] = None

    @property
    def llm(self):
        if self._llm is None:
            from app.llm.tools import get_tool_llm_client
            self._llm = get_tool_llm_client()
        return self._llm

    # ==================== 主入口 ====================

    def run(
        self,
        question: str,
        tenant_id: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        forced_route: Optional[Route] = None,
    ) -> Iterator[Dict[str, Any]]:
        """执行 Agent，逐事件 yield（可直接转 SSE）。

        Args:
            forced_route: 由调用方（API 层）预先算好的规则路由结果。
                          不传则内部计算。
        """
        state = AgentState(question=question, history=list(history or []))
        deadline = time.monotonic() + self.wall_clock
        t_start = time.monotonic()

        # ---- 关卡一：规则前置路由 ----
        decision = state.route = (
            RouteDecision(forced_route, "调用方指定") if forced_route
            else route_by_rules(question)
        )
        target = "sql" if decision.route == Route.SQL else (
            "rag" if decision.route == Route.RAG else "auto"
        )
        yield ev.ev_route(target, decision.reason)

        # ---- 强制路由：跳过 LLM 决策，直接调对应工具 ----
        if decision.route == Route.SQL:
            yield from self._run_forced_sql(state, tenant_id, deadline)
            yield from self._finalize(state, tenant_id, t_start)
            return
        if decision.route == Route.RAG:
            yield from self._run_forced_rag(state, tenant_id, deadline)
            yield from self._finalize(state, tenant_id, t_start)
            return

        # ---- 关卡二：LLM 工具选择循环（有界）----
        yield from self._run_llm_loop(state, tenant_id, deadline)
        yield from self._finalize(state, tenant_id, t_start)

    # ==================== 三条执行路径 ====================

    def _run_forced_sql(self, state: AgentState, tenant_id: Optional[str],
                        deadline: float) -> Iterator[Dict[str, Any]]:
        """规则强路由到 SQL：直接执行，省掉一次 LLM 决策（0 成本、低延迟）。"""
        call_id = f"forced-sql-{state.steps}"
        args = {"query": state.question}
        yield ev.ev_tool_call(call_id, "list_data_tables", {}, purpose="确认可用数据表")
        listing = self._exec_tool(state, "list_data_tables", {}, tenant_id, deadline)
        if listing is not None:
            yield listing

        # 用 Text2SQL 引擎生成并执行（它内部含 schema linking + few-shot + 自修正）
        yield ev.ev_tool_call(call_id, "sql_query", args, purpose=state.question)
        outcome = self._exec_sql_via_engine(state, tenant_id)
        yield ev.ev_tool_result(call_id, outcome.ok, outcome.summary,
                                **{k: v for k, v in outcome.payload.items()
                                   if k in ("row_count", "truncated", "elapsed_ms")})
        if outcome.payload.get("sql"):
            yield ev.ev_sql(
                outcome.payload["sql"],
                int(outcome.payload.get("row_count", 0)),
                int(outcome.payload.get("elapsed_ms", 0)),
                truncated=bool(outcome.payload.get("truncated")),
            )
        if outcome.payload.get("needs_clarification"):
            yield ev.ev_clarify(outcome.payload.get("clarification", ""))
        state.tool_calls.append({"name": "sql_query", "ok": outcome.ok})
        self._last_sql_observation = outcome.observation
        # 结果存下来供 _finalize 生成答案
        self._forced_observation = outcome

    def _run_forced_rag(self, state: AgentState, tenant_id: Optional[str],
                        deadline: float) -> Iterator[Dict[str, Any]]:
        """规则强路由到 RAG：直接检索 + 生成（复用既有链路能力）。"""
        call_id = f"forced-rag-{state.steps}"
        yield ev.ev_tool_call(call_id, "kb_search", {"query": state.question}, purpose="知识库检索")
        outcome = self._exec_tool(state, "kb_search", {"query": state.question}, tenant_id, deadline)
        if outcome is not None:
            yield outcome
        self._forced_observation = self._last_outcome

    def _run_llm_loop(self, state: AgentState, tenant_id: Optional[str],
                      deadline: float) -> Iterator[Dict[str, Any]]:
        """LLM 自主工具选择循环（有界）。"""
        messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for h in state.history[-6:]:
            messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        messages.append({"role": "user", "content": state.question})

        while state.steps < self.max_steps:
            # 三重上界检查
            if time.monotonic() > deadline:
                state.degraded, state.degraded_reason = True, "wall_clock"
                yield ev.ev_degraded("wall_clock", message="处理超时，已切换为直接检索回答")
                yield from self._run_forced_rag(state, tenant_id, deadline)
                return
            if state.llm_calls >= self.max_llm_calls:
                state.degraded, state.degraded_reason = True, "max_llm_calls"
                yield ev.ev_degraded("max_llm_calls", message="模型调用次数达上限")
                yield from self._run_forced_rag(state, tenant_id, deadline)
                return

            state.steps += 1
            state.llm_calls += 1

            try:
                chat = self.llm.chat_with_tools(messages, tools=TOOLS, temperature=0.1)
            except Exception as e:  # noqa: BLE001
                logger.error("[agent] LLM 调用失败: %s", e)
                state.degraded, state.degraded_reason = True, "llm_error"
                yield ev.ev_degraded("llm_error", message=str(e)[:200])
                yield from self._run_forced_rag(state, tenant_id, deadline)
                return

            # 无工具调用 -> 模型直接给答案，结束循环
            if not chat.tool_calls:
                state.final_answer = chat.content or ""
                return

            # 执行工具（可能多个）
            made_progress = False
            for tc in chat.tool_calls:
                args = tc.parse_args()

                # ---- 保护 4：相同工具 + 相同参数去重 ----
                key = f"{tc.name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
                if key in state.seen_calls:
                    logger.info("[agent] 检测到重复工具调用，判定为循环: %s", key[:120])
                    state.degraded, state.degraded_reason = True, "repeated_tool_call"
                    yield ev.ev_degraded("repeated_tool_call",
                                         message="检测到重复调用，已停止循环")
                    if not state.final_answer:
                        yield from self._run_forced_rag(state, tenant_id, deadline)
                    return
                state.seen_calls.add(key)

                yield ev.ev_tool_call(tc.id or f"call-{state.steps}", tc.name, args,
                                      purpose=str(args.get("purpose", "")))

                evt = self._exec_tool(state, tc.name, args, tenant_id, deadline)
                if evt is not None:
                    outcome = self._last_outcome
                    yield evt
                    made_progress = made_progress or outcome.ok
                    if tc.name == "sql_query" and outcome.payload.get("sql"):
                        yield ev.ev_sql(
                            outcome.payload["sql"],
                            int(outcome.payload.get("row_count", 0)),
                            int(outcome.payload.get("elapsed_ms", 0)),
                            truncated=bool(outcome.payload.get("truncated")),
                        )
                    # 工具结果回灌给模型，继续下一轮
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": tc.id or f"call-{state.steps}",
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }],
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id or f"call-{state.steps}",
                        "content": outcome.observation[:8000],   # 防御性截断，避免撑爆上下文
                    })

            if not made_progress:
                logger.info("[agent] 所有工具均失败，降级到 RAG")
                state.degraded, state.degraded_reason = True, "all_tools_failed"
                yield ev.ev_degraded("all_tools_failed", message="数据查询未成功，改用文档检索")
                yield from self._run_forced_rag(state, tenant_id, deadline)
                return

        # 步数用尽
        state.degraded, state.degraded_reason = True, "max_steps"
        yield ev.ev_degraded("max_steps", message="已达最大步数，直接作答")

    # ==================== 工具执行包装 ====================

    def _exec_tool(self, state: AgentState, name: str, args: Dict[str, Any],
                   tenant_id: Optional[str], deadline: float) -> Optional[Dict[str, Any]]:
        """执行工具并产出 tool_result 事件。结果同时存到 self._last_outcome。"""
        outcome = self.tools.dispatch(name, args, tenant_id=tenant_id)
        self._last_outcome = outcome
        state.tool_calls.append({"name": name, "ok": outcome.ok})

        # ⚠️ kb_search 一旦执行，必须补发标准 meta 帧（否则前端来源列表与
        #    「已隐藏 N 条」徽标会永久空着 —— 用户会以为功能退化了）。
        #    帧先暂存，由 _finalize 在 delta/done 之前统一发出。
        payload_evt: Dict[str, Any] = {}
        if name == "kb_search" and outcome.ok:
            p = outcome.payload
            self._pending_meta = ev.ev_meta(
                p.get("sources", []), int(p.get("retrieved_count", 0)), int(p.get("dropped", 0))
            )

        return ev.ev_tool_result(
            f"{name}-{state.steps}", outcome.ok, outcome.summary, **payload_evt,
        )

    def _exec_sql_via_engine(self, state: AgentState, tenant_id: Optional[str]) -> ToolOutcome:
        """用 Text2SQL 引擎处理自然语言问题（含 schema linking + few-shot + 自修正）。"""
        try:
            s = self.tools._session()
            try:
                res = self.tools.text2sql.answer(state.question, s, tenant_id=tenant_id)
            finally:
                s.close()
        except Exception as e:  # noqa: BLE001
            logger.error("[agent] text2sql 失败: %s", e)
            return ToolOutcome(ok=False, name="sql_query",
                               observation=f"数据查询失败：{str(e)[:200]}",
                               summary="数据查询失败")

        payload: Dict[str, Any] = {
            "sql": res.sql,
            "columns": res.columns,
            "rows": res.rows,
            "row_count": res.row_count,
            "elapsed_ms": res.elapsed_ms,
            "truncated": res.truncated,
            "needs_clarification": res.needs_clarification,
            "clarification": res.clarification,
            "attempts": res.attempts,
        }
        if res.needs_clarification:
            return ToolOutcome(
                ok=True, name="sql_query",
                observation=f"需要向用户澄清口径：{res.clarification}",
                summary="需要澄清统计口径", payload=payload,
            )
        if not res.ok:
            return ToolOutcome(
                ok=False, name="sql_query",
                observation=res.answer or "查询失败",
                summary="数据查询未成功",
                payload=payload,
            )
        return ToolOutcome(
            ok=True, name="sql_query",
            observation=self._render_sql_observation(res),
            summary=f"查询返回 {res.row_count} 行（{res.elapsed_ms}ms）"
                    + ("［已截断］" if res.truncated else ""),
            payload=payload,
        )

    @staticmethod
    def _render_sql_observation(res: Any) -> str:
        """把 SQL 结果渲染成给 LLM 的观察文本。"""
        if res.row_count == 0:
            return "查询执行成功但返回 0 行（可能是该条件下确实没有数据）。"
        header = " | ".join(str(c) for c in res.columns)
        lines = [header, "-" * len(header)]
        for r in res.rows[:50]:
            lines.append(" | ".join("" if v is None else str(v) for v in r))
        note = ""
        if res.truncated:
            note = ("\n\n⚠️ 结果已被行数上限截断，不是全部数据，"
                    "回答时必须说明这一点，或改用聚合查询。")
        return f"SQL：{res.sql}\n结果（{res.row_count} 行）：\n" + "\n".join(lines) + note

    # ==================== 收尾：生成最终答案并补发 meta ====================

    def _finalize(self, state: AgentState, tenant_id: Optional[str],
                  t_start: float) -> Iterator[Dict[str, Any]]:
        """产出最终答案。**先补发 meta 帧**（若有检索），再 delta + done。"""
        # 补发 meta（kb_search 曾执行过）—— 保证前端来源列表与徽标正常
        if self._pending_meta:
            yield self._pending_meta
            self._pending_meta = None

        if state.final_answer is None:
            state.final_answer = self._compose_answer(state)

        answer = state.final_answer or "抱歉，未能找到相关信息。"
        # 逐段 yield delta 便于前端保持既有渲染路径
        yield ev.ev_delta(answer)
        self.last_stats = {
            "steps": state.steps,
            "llm_calls": state.llm_calls,
            "tool_calls": len(state.tool_calls),
            "degraded": state.degraded,
            "degraded_reason": state.degraded_reason,
            "elapsed_ms": int((time.monotonic() - t_start) * 1000),
        }
        yield ev.ev_done(answer, agent_stats=self.last_stats)

    def _compose_answer(self, state: AgentState) -> str:
        """用模型把工具观察组织成自然语言答案。"""
        obs = self._forced_observation
        if obs is not None and not obs.ok:
            return obs.observation or "未能获取到所需数据。"

        # 澄清优先：直接把反问作为答案
        if obs is not None and obs.payload.get("needs_clarification"):
            clar = obs.payload.get("clarification") or ""
            return clar or "请补充统计口径。"

        observation = obs.observation if obs is not None else "（无可用资料）"

        if state.llm_calls >= self.max_llm_calls:
            # 成本上界已到：不再调 LLM，直接回观察文本（保证有答案）
            return observation[:2000]

        prompt = (
            f"用户问题：{state.question}\n\n"
            f"工具返回的资料：\n{observation[:8000]}\n\n"
            "请基于以上资料用简洁的中文回答用户问题。"
            "要求：直接给结论与关键数字；不要重复工具原始输出；"
            "资料里没有的信息不要编造；如果资料不足以回答，就直说未找到。"
        )
        try:
            state.llm_calls += 1
            chat = self.llm.chat_with_tools(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": prompt}],
                tools=None, temperature=0.2,
            )
            if chat.content:
                return chat.content
        except Exception as e:  # noqa: BLE001
            logger.warning("[agent] 答案生成失败，回退为原始观察: %s", e)
        return observation[:2000]


_loop: Optional[AgentLoop] = None


def get_agent_loop() -> AgentLoop:
    global _loop
    if _loop is None:
        _loop = AgentLoop()
    return _loop
