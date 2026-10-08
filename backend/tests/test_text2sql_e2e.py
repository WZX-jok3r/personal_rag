"""Text2SQL 引擎端到端测试（**真连 LLM + PostgreSQL**）。

这些是真实验证：自然语言 -> SQL -> 执行 -> 结果。
不加 mock，因为本阶段要证明的正是"这条链路在真实模型上能跑对"。

用 skipif 保护：缺 API Key / 缺数据库时自动跳过，不拖红套件。

成本提示：每条用例一次 LLM 调用，约 25 条 => 约 25 次调用。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.analytics.text2sql import Text2SqlEngine, get_text2sql_engine
from app.core.config import settings


def _stack_available() -> bool:
    if not settings.siliconflow_api_key:
        return False
    try:
        e = create_engine(settings.sync_analytics_ro_url, connect_args={"connect_timeout": 5})
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        e.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _stack_available(),
    reason="需要 SILICONFLOW_API_KEY + kb_analytics（LLM + 数据库真连测试）",
)


@pytest.fixture(scope="module")
def rag_engine():
    e = create_engine(settings.sync_postgres_url)
    yield e
    e.dispose()


@pytest.fixture(scope="module")
def engine() -> Text2SqlEngine:
    return get_text2sql_engine()


def ask(engine: Text2SqlEngine, rag_engine, question: str):
    with Session(rag_engine) as s:
        return engine.answer(question, s)


def _numbers(res) -> list:
    """把结果里的数值都摊平，便于断言"答案包含某个数字"。"""
    out = []
    for r in res.rows:
        for v in r:
            if isinstance(v, (int, float)):
                out.append(v)
    return out


class TestHeadlineCases:
    """改造方案第三部分里 RAG 答错或答不准的题，SQL 必须答对。"""

    def test_department_headcount(self, engine, rag_engine):
        """「Sales 和 Engineering 各多少人」——RAG 曾答 Engineering=25（真值 1010）。"""
        res = ask(engine, rag_engine, "员工表里 Sales 部门和 Engineering 部门各有多少人？")
        assert res.ok, f"执行失败: {res.answer} | errors={res.errors}"
        nums = _numbers(res)
        assert 1042 in nums, f"缺 Sales=1042，实际 {nums}"
        assert 1010 in nums, f"缺 Engineering=1010，实际 {nums}"

    def test_department_difference(self, engine, rag_engine):
        """差值题：RAG 曾答 1017，真值 32。"""
        res = ask(engine, rag_engine, "Sales 部门比 Engineering 部门多多少人？")
        assert res.ok, f"执行失败: {res.answer} | errors={res.errors}"
        assert 32 in _numbers(res), f"期望差值 32，实际 {_numbers(res)}"

    def test_max_salary_person(self, engine, rag_engine):
        res = ask(engine, rag_engine, "全公司薪资最高的员工是谁，在哪个部门？")
        assert res.ok, f"执行失败: {res.answer}"
        nums = _numbers(res)
        assert 179997 in nums, f"期望薪资 179997，实际 {nums}"

    def test_2026_hires(self, engine, rag_engine):
        res = ask(engine, rag_engine, "2026 年入职的员工有多少人？")
        assert res.ok, f"执行失败: {res.answer}"
        assert 192 in _numbers(res), f"期望 192，实际 {_numbers(res)}"

    def test_most_department(self, engine, rag_engine):
        res = ask(engine, rag_engine, "哪个部门人数最多？有多少人？")
        assert res.ok, f"执行失败: {res.answer}"
        assert 1042 in _numbers(res)


class TestSalesAndExpenses:
    def test_top_revenue_product(self, engine, rag_engine):
        res = ask(engine, rag_engine, "哪个产品的总营收最高？")
        assert res.ok, f"执行失败: {res.answer}"

    def test_total_sales_count(self, engine, rag_engine):
        res = ask(engine, rag_engine, "销售流水一共有多少条记录？")
        assert res.ok, f"执行失败: {res.answer}"
        assert 20 in _numbers(res)

    def test_expense_total(self, engine, rag_engine):
        res = ask(engine, rag_engine, "费用支出总金额是多少？")
        assert res.ok, f"执行失败: {res.answer}"
        # 真值 34206.68（Summary sheet 给出），允许浮点
        nums = _numbers(res)
        assert any(abs(n - 34206.68) < 1 for n in nums), f"期望约 34206.68，实际 {nums}"


class TestChineseColumnTable:
    """中文列名表（cost_data）—— 验证 col_N 标识符方案可用。"""

    def test_count(self, engine, rag_engine):
        res = ask(engine, rag_engine, "橱柜成本明细表里有多少条记录？")
        assert res.ok, f"执行失败: {res.answer}"
        assert 6 in _numbers(res)

    def test_most_expensive(self, engine, rag_engine):
        res = ask(engine, rag_engine, "橱柜成本表里单价最贵的项目是哪个？")
        assert res.ok, f"执行失败: {res.answer}"
        # 最高单价 210（PET肤感门板）
        assert 210 in _numbers(res), f"期望 210，实际 {_numbers(res)}"


class TestAmbiguityHandling:
    """口径歧义必须反问，不能猜 —— 这是"消灭静默错误"的延伸。"""

    def test_ambiguous_metric_triggers_clarification(self, engine, rag_engine):
        res = ask(engine, rag_engine, "平均客单价是多少？")
        assert res.needs_clarification, f"应触发口径澄清，实际 answer={res.answer!r}"
        assert res.clarification
        # 反问里要给出候选口径，而不是空泛地说"请补充"
        assert "口径" in res.clarification

    def test_clarified_question_proceeds(self, engine, rag_engine):
        """带上口径后不应再反问（历史里已有解释）。"""
        res = ask(engine, rag_engine, "sales 表里按每笔订单算，平均营收是多少？")
        # 不应因"客单价"歧义被拦（这里没提客单价），应正常执行
        assert not res.needs_clarification
        assert res.ok, f"执行失败: {res.answer}"


class TestSafetyIntegration:
    """安全网在真实链路里也必须生效。"""

    def test_generated_sql_is_always_single_select(self, engine, rag_engine):
        """生成的 SQL 必须通过 guard（能被 guard 放行即说明是单条 SELECT）。"""
        from app.analytics.guard import guard_sql

        for q in ["有多少员工？", "哪个部门人数最多？", "薪资最高的前 3 人是谁？"]:
            res = ask(engine, rag_engine, q)
            if res.ok and res.sql:
                # 已是 guard 改写后的语句，应能被 sqlglot 解析且是 SELECT
                assert res.sql.strip().upper().startswith("SELECT"), res.sql
                # 必须带被注入的 LIMIT（外层包裹）
                assert "LIMIT" in res.sql.upper(), res.sql

    def test_row_limit_is_enforced(self, engine, rag_engine):
        """要求"列出所有人"时必须被截断并标注。"""
        res = ask(engine, rag_engine, "把所有员工的姓名都列出来")
        if res.ok:
            assert res.row_count <= settings.sql_max_rows
            if res.row_count >= settings.sql_max_rows:
                assert res.truncated, "达到上限时必须标注截断"


class TestSelfCorrection:
    """自修正：报错回灌后应能重写成功。

    难以稳定构造"第一次必错"的场景，所以这里只验证失败路径的
    结构化输出是完整的（errors 列表被填充、answer 可读）。
    """

    def test_bad_question_returns_readable_result(self, engine, rag_engine):
        res = ask(engine, rag_engine, "请查询一个根本不存在的表里的数据")
        # 要么成功（模型找到别的表），要么给出可读的失败说明 + 错误明细
        if not res.ok:
            assert res.answer
            assert res.attempts >= 1


class TestSchemaLinkingIsUsed:
    def test_relevant_table_selected(self, engine, rag_engine):
        """问员工相关问题时，schema 里应包含 employees 表。"""
        from app.analytics import schema as sm

        with Session(rag_engine) as s:
            schemas = sm.load_table_schemas(s)
            text_out = sm.select_relevant_schema_text("哪个部门人数最多？", schemas, max_tables=2)
        assert "employees" in text_out

    def test_fewshot_retrieval_finds_similar(self):
        from app.analytics import fewshot as fs

        ex = fs.retrieve("哪个部门人最多", fs.get_examples(), k=2)
        assert ex, "应能召回至少一条示例"
        assert any("count" in e.sql.lower() for e in ex)
