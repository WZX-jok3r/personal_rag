"""eval.compare 的单元测试 —— 验证回归闸口"真的会拦"。

为什么必须测这个：一个永远返回"无回归"的闸口比没有闸口更危险
（会让人误以为改造是安全的）。所以这里用合成数据构造四类回归，
断言 compare() 都能检出。
"""

from __future__ import annotations

from eval.compare import _rank_bad, compare


def _mk(summary: dict, results: list[dict]) -> dict:
    """构造 compare() 期望的输入结构。"""
    return {"summary": summary, "results": results, "report": {"summary": summary}}


def _q(question: str, recall: bool = True, rank: int = 1, passed: bool = True) -> dict:
    return {
        "question": question,
        "expected_source": "a.md",
        "recall_at_k": recall,
        "source_rank": rank,
        "passed": passed,
    }


BASE_SUM = {"pass_rate": 1.0, "recall_at_k_rate": 1.0, "mrr": 1.0}


class TestAggregateRegression:
    def test_identical_is_clean(self):
        b = _mk(BASE_SUM, [_q("q1"), _q("q2")])
        c = _mk(BASE_SUM, [_q("q1"), _q("q2")])
        has_reg, regs, _ = compare(b, c)
        assert not has_reg, regs

    def test_recall_drop_detected(self):
        b = _mk(BASE_SUM, [_q("q1")])
        c = _mk({**BASE_SUM, "recall_at_k_rate": 0.5}, [_q("q1", recall=False, rank=-1)])
        has_reg, regs, _ = compare(b, c)
        assert has_reg
        assert any("丢召回" in r or "Recall@K" in r for r in regs)

    def test_mrr_drop_detected(self):
        b = _mk(BASE_SUM, [_q("q1")])
        c = _mk({**BASE_SUM, "mrr": 0.5}, [_q("q1")])
        has_reg, regs, _ = compare(b, c)
        assert has_reg
        assert any("MRR" in r for r in regs)

    def test_improvement_is_not_regression(self):
        b = _mk({**BASE_SUM, "mrr": 0.5}, [_q("q1")])
        c = _mk(BASE_SUM, [_q("q1")])
        has_reg, _, imps = compare(b, c)
        assert not has_reg
        assert any("MRR" in i for i in imps)


class TestPerQuestionRegression:
    def test_equal_aggregate_but_swapped_is_caught(self):
        """核心场景：聚合指标完全不变，但题目 A 修好、题目 B 坏了。

        只看聚合指标会漏掉这种"等量置换"，逐题比对必须抓到。
        """
        b = _mk(BASE_SUM, [_q("q1", rank=1), _q("q2", recall=False, rank=-1)])
        c = _mk(BASE_SUM, [_q("q1", recall=False, rank=-1), _q("q2", rank=1)])
        has_reg, regs, _ = compare(b, c)
        assert has_reg, "聚合持平时仍应检出 q1 的召回丢失"
        assert any("q1" in r for r in regs)

    def test_rank_downgrade_detected(self):
        b = _mk(BASE_SUM, [_q("q1", rank=1)])
        c = _mk(BASE_SUM, [_q("q1", rank=4)])
        has_reg, regs, _ = compare(b, c)
        assert has_reg
        assert any("排名下降" in r for r in regs)

    def test_rank_upgrade_is_improvement(self):
        b = _mk(BASE_SUM, [_q("q1", rank=4)])
        c = _mk(BASE_SUM, [_q("q1", rank=1)])
        has_reg, _, imps = compare(b, c)
        assert not has_reg
        assert any("排名上升" in i for i in imps)

    def test_missing_question_detected(self):
        b = _mk(BASE_SUM, [_q("q1"), _q("q2")])
        c = _mk(BASE_SUM, [_q("q1")])
        has_reg, regs, _ = compare(b, c)
        assert has_reg
        assert any("缺题" in r for r in regs)

    def test_passed_to_failed_detected(self):
        b = _mk(BASE_SUM, [_q("q1", passed=True)])
        c = _mk(BASE_SUM, [_q("q1", passed=False)])
        has_reg, regs, _ = compare(b, c)
        assert has_reg
        assert any("通过→失败" in r for r in regs)


class TestRankHelper:
    def test_rank_bad_semantics(self):
        assert not _rank_bad(1, 1)
        assert _rank_bad(1, 5)          # 变差
        assert not _rank_bad(5, 1)      # 变好
        assert _rank_bad(3, -1)         # 未召回 = 最差
        assert not _rank_bad(-1, 3)     # 从缺到有 = 变好
        assert not _rank_bad(-1, -1)
