"""口径歧义检测的单元测试（纯规则，无 LLM 调用）。

背景（实测得出的教训，见 docs/经验教训.md L-011）：
初版把「平均薪资」一律判为歧义，导致
    「Sales 部门的平均薪资是多少？」
    「全公司平均薪资是多少？」
这类**问题本身已把口径说清楚**的题被拦下反问。
过度澄清比不澄清更糟 —— 它把本来能答的问题变成答不了。

因此本文件的核心是**双向**断言：
    - 真歧义必须被拦（否则会给出"看起来对但口径错"的数字）
    - 假歧义必须放行（否则功能不可用）
"""

from __future__ import annotations

import pytest

from app.analytics.text2sql import Text2SqlEngine


@pytest.fixture
def engine() -> Text2SqlEngine:
    # 只测纯规则方法，不触发 LLM / DB；executor 用 None 占位不会被执行
    e = Text2SqlEngine.__new__(Text2SqlEngine)
    return e


class TestTrueAmbiguityIsCaught:
    """真歧义：必须反问，不能猜。"""

    @pytest.mark.parametrize("q,keyword", [
        ("平均客单价是多少？", "客单价"),
        ("客单价多少", "客单价"),
        ("这个月的增长率是多少？", "增长率"),
        ("转化率是多少", "转化率"),
        ("总额是多少", "总额"),
    ])
    def test_strong_ambiguous_metrics(self, engine, q, keyword):
        out = engine.detect_ambiguity(q)
        assert out is not None, f"应触发澄清: {q}"
        assert keyword in out
        assert "口径" in out

    def test_unqualified_avg_salary_is_ambiguous(self, engine):
        """未限定范围的「平均薪资」：按人头平均 vs 部门平均再平均，结果不同。"""
        out = engine.detect_ambiguity("公司平均薪资是多少？")
        assert out is not None
        assert "平均薪资" in out

    def test_clarification_offers_concrete_options(self, engine):
        """反问必须给出**具体候选口径**，不能只说"请补充说明"。"""
        out = engine.detect_ambiguity("平均客单价是多少？")
        assert out is not None
        assert "avg(revenue)" in out or "sum(revenue)" in out


class TestFalsePositivesAreAvoided:
    """假歧义：必须放行（这是初版的缺陷，已修）。"""

    @pytest.mark.parametrize("q", [
        "Sales 部门的平均薪资是多少？",
        "全公司平均薪资是多少？",
        "整体平均工资是多少？",
        "各部门的平均薪资分别是多少？",
        "按部门统计平均薪资",
        "所有员工的平均薪资",
        "每个部门的平均薪资是多少？",
    ])
    def test_scoped_avg_salary_passes(self, engine, q):
        out = engine.detect_ambiguity(q)
        assert out is None, f"不应触发澄清（已限定范围）: {q} -> {out}"

    @pytest.mark.parametrize("q", [
        "员工表里有多少人？",
        "哪个部门人数最多？",
        "薪资最高的员工是谁？",
        "2026 年入职多少人？",
        "橱柜成本里单价最高的项目是哪个？",
        "哪个产品的总营收最高？",
    ])
    def test_normal_questions_pass(self, engine, q):
        assert engine.detect_ambiguity(q) is None, f"普通问题不应触发澄清: {q}"

    def test_explicit_caliber_word_passes(self, engine):
        """问题里自带"按/口径/每笔"等限定词时不追问。"""
        for q in ["平均客单价按每笔订单算", "客单价的口径按每笔营收", "平均每笔客单价是多少"]:
            assert engine.detect_ambiguity(q) is None, q


class TestHistorySuppression:
    """已澄清过的不应反复追问（否则用户永远走不出澄清循环）。"""

    def test_no_repeat_after_clarification(self, engine):
        q = "平均客单价是多少？"
        assert engine.detect_ambiguity(q) is not None

        # 模拟：历史上已经问过并把候选口径告诉了用户
        history = [
            {"role": "assistant", "content": engine.detect_ambiguity(q)},
            {"role": "user", "content": "按 sum(revenue)/sum(quantity) 算"},
        ]
        assert engine.detect_ambiguity(q, history) is None, "已澄清过不应再问"

    def test_unrelated_history_does_not_suppress(self, engine):
        history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好，请问需要什么？"}]
        assert engine.detect_ambiguity("平均客单价是多少？", history) is not None
