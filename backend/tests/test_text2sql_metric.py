"""Text2SQL 评测比较逻辑的单元测试。

为什么这个文件重要：**评测指标本身出错比模型答错更隐蔽**。
本次实测中，初版"严格相等"判定把 15 个语义正确的答案判成了错误
（模型多返回了识别性列，如 (Operations, Derek, Cummings, 179997) vs (Operations,)）。
只有把比较规则用测试钉死，才能保证 EX 这个数字可信 ——
一个错的指标会让人去"修"本来正确的模型。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from eval.text2sql_runner import (
    _row_covers,
    normalize_rows,
    normalize_value,
    results_equal,
)


class TestNormalizeValue:
    @pytest.mark.parametrize("raw,expected", [
        (None, None),
        (1042, 1042),
        (1042.0, 1042),                 # 浮点整数 -> 整数
        (Decimal("1042"), 1042),        # Decimal 整数
        (Decimal("1042.50"), 1042.5),   # Decimal 小数
        ("1042", 1042),                 # 数字字符串
        ("1042.0", 1042),
        ("Sales", "Sales"),             # 普通字符串
        (" Sales ", "Sales"),           # 去空白
        (True, True),                   # 布尔不转数字
        (date(2026, 8, 2), "2026-08-02"),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_value(raw) == expected

    def test_float_precision_is_rounded(self):
        assert normalize_value(47044.680000001) == normalize_value(47044.68)


class TestRowCovers:
    """生成行覆盖期望行（多带列算对，缺列算错）。"""

    def test_equal_rows(self):
        assert _row_covers(("Sales", 1042), ("Sales", 1042))

    def test_extra_trailing_columns_ok(self):
        """本项目的实际场景：模型多返回识别性列。"""
        assert _row_covers(("Operations", "Derek", "Cummings", 179997), ("Operations",))

    def test_extra_columns_in_middle_ok(self):
        assert _row_covers(("Derek", "Cummings", "Operations", 179997), ("Derek", "Cummings"))

    def test_missing_column_fails(self):
        """生成结果缺少期望列 —— 真错。"""
        assert not _row_covers(("Sales",), ("Sales", 1042))

    def test_wrong_value_fails(self):
        assert not _row_covers(("Marketing", 1042), ("Sales", 1042))

    def test_wrong_count_fails(self):
        assert not _row_covers(("Sales", 999), ("Sales", 1042))

    def test_column_order_matters(self):
        """列序颠倒通常意味着选错了列，不能判对。"""
        assert not _row_covers(("Operations", "Sales"), ("Sales", "Operations"))


class TestResultsEqual:
    def test_empty_equal(self):
        assert results_equal([], [], ordered=False)

    def test_length_mismatch_fails(self):
        assert not results_equal([("Sales",)], [("Sales",), ("HR",)], ordered=False)

    def test_ordered_exact(self):
        a = [("A", 1), ("B", 2)]
        assert results_equal(a, a, ordered=True)

    def test_ordered_detects_swap(self):
        a = [("A", 1), ("B", 2)]
        b = [("B", 2), ("A", 1)]
        assert not results_equal(a, b, ordered=True), "有序题必须能发现行序颠倒"
        assert results_equal(a, b, ordered=False), "无序题不应关心行序"

    def test_unordered_with_extra_columns(self):
        """真实场景：Top-N 题模型多带列。"""
        generated = [
            ("PET肤感门板", 210),
            ("多层板地柜板材", 182),
            ("颗粒板柜体板材", 135),
        ]
        gold = [("PET肤感门板",), ("多层板地柜板材",), ("颗粒板柜体板材",)]
        assert results_equal(generated, gold, ordered=True)

    def test_ordered_with_extra_columns_wrong_order_fails(self):
        generated = [("B", 2), ("A", 1)]
        gold = [("A",), ("B",)]
        assert not results_equal(generated, gold, ordered=True)

    def test_unordered_multiset_matching(self):
        """重复行必须按多重集匹配，不能一行匹配多条。"""
        generated = [("Sales",), ("Sales",), ("HR",)]
        gold = [("Sales",), ("HR",)]
        assert not results_equal(generated, gold, ordered=False)

    def test_duplicate_rows_correct_count(self):
        generated = [("Sales",), ("Sales",), ("HR",)]
        gold = [("Sales",), ("Sales",), ("HR",)]
        assert results_equal(generated, gold, ordered=False)

    def test_decimal_vs_int(self):
        assert results_equal([(Decimal("1042"),)], [(1042,)], ordered=True)

    def test_none_handling(self):
        assert results_equal([(None,)], [(None,)], ordered=True)
        assert not results_equal([(None,)], [(0,)], ordered=True)


class TestNormalizeRows:
    def test_normalizes_all_cells(self):
        rows = [(Decimal("1.50"), " x ", date(2026, 1, 2))]
        assert normalize_rows(rows) == [(1.5, "x", "2026-01-02")]


class TestRealRegressionFromEvaluation:
    """把实测中误判的 15 个案例固化成回归测试。

    这些答案在初版指标下被判错，但语义完全正确。
    固化下来，防止将来有人"修"指标时又把它们判错。
    """

    CASES = [
        # (说明, 生成结果, 期望结果)
        ("部门人数最多", [("Sales", 1042)], [("Sales",)]),
        ("薪资最高员工在哪个部门", [("Operations", "Derek", "Cummings", 179997)], [("Operations",)]),
        ("Operations 人数", [("Operations", 972)], [(972,)]),
        ("总营收最高的产品", [("MegaPack", 47044.68)], [("MegaPack",)]),
        ("单价最高的项目", [("PET肤感门板", 210)], [("PET肤感门板",)]),
        ("最早入职员工", [("Marty", "Farrell", "Operations", "2011-04-06")], [("Marty", "Farrell")]),
        ("工号 42 姓名", [(42, "Madelynn", "Steuber", "x@y.com", "Engineering", 46479, "2017-07-31")],
         [("Madelynn", "Steuber")]),
    ]

    @pytest.mark.parametrize("label,generated,gold", CASES, ids=[c[0] for c in CASES])
    def test_was_false_negative_now_correct(self, label, generated, gold):
        assert results_equal(generated, gold, ordered=False), f"{label} 被误判"

    def test_multi_row_name_query(self):
        """名字叫 Derek 的员工：模型返回 3 人且带部门薪资，期望只要部门。"""
        generated = [
            ("Derek", "Rice", "Design", 98002),
            ("Derek", "Luettgen", "Sales", 179411),
            ("Derek", "Cummings", "Operations", 179997),
        ]
        gold = [("Design",), ("Sales",), ("Operations",)]
        assert results_equal(generated, gold, ordered=False)
