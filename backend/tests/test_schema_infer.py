"""schema_infer 单元测试。

重点覆盖三类"会静默产生错误 schema"的场景：
1. 表头探测（cost_data.xlsx 的标题行陷阱）
2. 类型推断的全列判据（抽样会导致不确定）
3. 标识符生成的安全性与唯一性（中文列名 / 冲突 / 注入）
"""

from __future__ import annotations

import pytest

from app.analytics.schema_infer import (
    detect_header_row,
    infer_column_type,
    infer_schema,
    is_sequence_col,
    looks_like_header_row,
    looks_like_date,
    make_identifier,
    normalize_date,
    normalize_table_name,
    parse_number,
)


class TestParseNumber:
    @pytest.mark.parametrize("raw,expected", [
        ("28", 28.0),
        ("6623.76", 6623.76),
        ("1,234.5", 1234.5),
        ("$124,328", 124328.0),
        ("￥210", 210.0),
        ("", None),
        ("abc", None),
        ("12abc", None),
    ])
    def test_parse(self, raw, expected):
        assert parse_number(raw) == expected

    def test_none(self):
        assert parse_number(None) is None


class TestSequenceCol:
    def test_by_header_name(self):
        assert is_sequence_col("ID", ["7", "3", "9"])
        assert is_sequence_col("序号", ["x", "y"])

    def test_by_contiguous_values(self):
        assert is_sequence_col("whatever", ["1", "2", "3", "4"])

    def test_not_sequence_when_gaps(self):
        assert not is_sequence_col("whatever", ["1", "2", "5"])

    def test_not_sequence_when_text(self):
        assert not is_sequence_col("name", ["a", "b"])

    def test_salary_is_not_sequence(self):
        """真实数据：薪资列是整数但不连续，绝不能当主键。"""
        assert not is_sequence_col("Salary", ["79701", "141914", "179997"])


class TestDate:
    @pytest.mark.parametrize("v", [
        "2026-08-02", "2026-08-02 00:00:00", "2026/8/2", "2026/08/02 0:00",
    ])
    def test_looks_like_date(self, v):
        assert looks_like_date(v)

    @pytest.mark.parametrize("v", ["179997", "Gadget Plus", "2026-08", ""])
    def test_not_date(self, v):
        assert not looks_like_date(v)

    def test_normalize_pads_zeros(self):
        assert normalize_date("2026/8/2") == "2026-08-02"
        assert normalize_date("2026-08-02 00:00:00") == "2026-08-02"
        assert normalize_date("garbage") is None


class TestHeaderDetection:
    def test_title_row_trap_cost_data(self):
        """真实陷阱：cost_data.xlsx 第 0 行是大标题，第 1 行才是表头。

        如果误判为第 0 行，会建出一张只有 1 个有效列的表（静默错误）。
        """
        rows = [
            ["橱柜项目成本明细表", "", "", "", "", "", "", ""],
            ["项目", "品牌/型号", "规格/用途", "单价(元)", "数量", "单位", "计费方式", "备注"],
            ["铰链", "悍高 三段力", "柜门阻尼缓冲", "28", "20", "个", "按个计费", "具备阻尼功能"],
        ]
        assert detect_header_row(rows) == 1

    def test_normal_header_first_row(self):
        """Employees：第 0 行就是表头。"""
        rows = [
            ["ID", "First Name", "Last Name", "Email", "Department", "Salary", "Hire Date"],
            ["1", "Jordan", "Nienow", "p@x.com", "HR", "79701", "2016-08-02"],
            ["2", "Garrett", "Rath", "d@x.com", "Support", "141914", "2020-08-22"],
        ]
        assert detect_header_row(rows) == 0

    def test_single_cell_row_is_not_header(self):
        assert not looks_like_header_row(["只有一格", "", ""])

    def test_row_with_numbers_is_not_header(self):
        assert not looks_like_header_row(["1", "2", "3"])

    def test_row_with_duplicates_is_not_header(self):
        assert not looks_like_header_row(["A", "A", "B"])

    def test_all_garbage_falls_back_to_zero(self):
        """找不到像表头的行时回退到 0（保持既有 loader 行为，不引入新行为）。"""
        assert detect_header_row([["1", "2"], ["3", "4"]]) == 0

    def test_empty_rows(self):
        assert detect_header_row([]) == 0


class TestIdentifier:
    def test_ascii_to_snake_case(self):
        used: set[str] = set()
        assert make_identifier("First Name", 0, used) == "first_name"
        assert make_identifier("Hire Date", 1, used) == "hire_date"

    def test_chinese_falls_back_to_positional(self):
        used: set[str] = set()
        assert make_identifier("单价(元)", 3, used) == "col_4"
        assert make_identifier("品牌/型号", 1, used) == "col_2"

    def test_duplicates_get_suffix(self):
        used: set[str] = set()
        assert make_identifier("Name", 0, used) == "name"
        assert make_identifier("Name", 1, used) == "name_2"
        assert make_identifier("Name", 2, used) == "name_3"

    def test_injection_attempt_is_neutralized(self):
        """标识符注入：恶意表头必须只留下安全字符。"""
        used: set[str] = set()
        out = make_identifier('a"; DROP TABLE x; --', 0, used)
        assert out == "a_drop_table_x"
        assert all(c.isalnum() or c == "_" for c in out)

    def test_starts_with_digit_is_fixed(self):
        used: set[str] = set()
        assert make_identifier("2026 revenue", 0, used) == "col_1"

    def test_result_always_safe_charset(self):
        used: set[str] = set()
        for i, h in enumerate(["正常", "A B", "！@#", "x-y", ""]):
            out = make_identifier(h, i, used)
            assert all(c.isalnum() or c == "_" for c in out), out


class TestInferColumnType:
    def test_primary_key(self):
        t, pk = infer_column_type(["1", "2", "3", "4"], "ID")
        assert (t, pk) == ("INTEGER", True)

    def test_integer(self):
        t, pk = infer_column_type(["79701", "141914"], "Salary")
        assert (t, pk) == ("INTEGER", False)

    def test_numeric_with_decimals(self):
        t, pk = infer_column_type(["6623.76", "3596.64"], "Revenue")
        assert (t, pk) == ("NUMERIC(14,2)", False)

    def test_date(self):
        t, pk = infer_column_type(["2016-08-02", "2020-08-22"], "Hire Date")
        assert (t, pk) == ("DATE", False)

    def test_low_cardinality_text(self):
        t, pk = infer_column_type(["Sales", "HR", "Sales"], "Department")
        assert t == "VARCHAR(64)"

    def test_long_text(self):
        long = "x" * 200
        t, pk = infer_column_type([long, long + "y"], "Note")
        assert t == "TEXT"

    def test_empty_column_is_text(self):
        assert infer_column_type(["", "", ""], "Whatever") == ("TEXT", False)

    def test_mixed_int_and_text_degrades_to_varchar(self):
        """全列判据：只要有一个非数值，就不能判成数值列。"""
        t, _ = infer_column_type(["1", "2", "N/A"], "Qty")
        assert t.startswith("VARCHAR")

    def test_all_values_checked_not_sampled(self):
        """关键性质：第 4 个值非法也必须被发现（不抽样）。"""
        t, _ = infer_column_type(["1", "2", "3", "oops"], "Qty")
        assert t.startswith("VARCHAR")


class TestInferSchema:
    def test_employees_like_table(self):
        rows = [
            ["ID", "First Name", "Last Name", "Email", "Department", "Salary", "Hire Date"],
            ["1", "Jordan", "Nienow", "p@x.com", "HR", "79701", "2016-08-02"],
            ["2", "Garrett", "Rath", "d@x.com", "Support", "141914", "2020-08-22"],
            ["3", "Ada", "Lovelace", "a@x.com", "HR", "90000", "2020-08-22"],
        ]
        s = infer_schema(rows, "employees", "员工表", "员工花名册")

        assert s.header_row_index == 0
        assert s.data_rows == 3
        assert s.column_names == [
            "id", "first_name", "last_name", "email", "department", "salary", "hire_date",
        ]
        by_name = {c.name: c for c in s.columns}
        assert by_name["id"].is_primary_key
        assert by_name["salary"].sql_type == "INTEGER"
        assert by_name["hire_date"].sql_type == "DATE"
        assert by_name["department"].enum_values == ["HR", "Support"]
        # 枚举值必须出现在 comment 里（这是准确率的关键）
        assert "HR" in by_name["department"].comment()

    def test_cost_data_with_title_row(self):
        rows = [
            ["橱柜项目成本明细表", "", "", "", "", "", "", ""],
            ["项目", "品牌/型号", "规格/用途", "单价(元)", "数量", "单位", "计费方式", "备注"],
            ["铰链", "悍高 三段力", "柜门阻尼缓冲", "28", "20", "个", "按个计费", "具备阻尼功能"],
            ["板材", "兔宝宝", "颗粒板柜体", "210", "3", "张", "按张计费", ""],
        ]
        s = infer_schema(rows, "cost_data", "橱柜成本明细")

        assert s.header_row_index == 1, "必须跳过标题行"
        assert s.data_rows == 2
        assert len(s.columns) == 8, "必须推断出 8 列（不是 1 列）"
        # 标题行被记录，便于审计
        assert s.header_row_skipped == [["橱柜项目成本明细表", "", "", "", "", "", "", ""]]
        by_name = {c.name: c for c in s.columns}
        # 中文表头保留在 description 里（进 COMMENT 与 prompt）
        assert by_name["col_1"].source_header == "项目"
        assert "项目" in by_name["col_1"].description
        # 注意：这里用的样本值是 "28"/"210"/"20"/"3" —— 全是整数，
        # 因此推断为 INTEGER 才是正确行为（不能被列名里的"单价(元)"带偏）。
        assert by_name["col_4"].sql_type == "INTEGER"
        assert by_name["col_5"].sql_type == "INTEGER"

    def test_decimal_and_integer_columns_differ(self):
        """区分"整数值列"与"小数值列"——判据是全列字面量形态，不是列名。"""
        rows = [
            ["Item", "UnitPrice", "Qty"],
            ["a", "28", "20"],
            ["b", "210.50", "3"],
        ]
        by_name = {c.name: c for c in infer_schema(rows, "t").columns}
        assert by_name["unitprice"].sql_type == "NUMERIC(14,2)"   # 有小数
        assert by_name["qty"].sql_type == "INTEGER"               # 全整数

    def test_ragged_rows_are_normalized(self):
        rows = [
            ["A", "B", "C"],
            ["1", "2"],           # 短行
            ["3", "4", "5", "6"],  # 长行
        ]
        s = infer_schema(rows, "t")
        assert len(s.columns) == 3
        assert s.data_rows == 2

    def test_empty_rows_skipped(self):
        rows = [["A", "B"], ["", ""], ["1", "2"]]
        s = infer_schema(rows, "t")
        assert s.data_rows == 1

    def test_empty_input(self):
        s = infer_schema([], "t")
        assert s.columns == []
        assert s.data_rows == 0

    def test_all_null_column(self):
        rows = [["A", "B"], ["1", ""], ["2", ""]]
        s = infer_schema(rows, "t")
        by_name = {c.name: c for c in s.columns}
        assert by_name["b"].sql_type == "TEXT"
        assert by_name["b"].nullable


class TestNormalizeTableName:
    @pytest.mark.parametrize("src,sheet,expected", [
        ("xlsx-sample-large-10000-rows.xlsx", "Employees", "employees"),
        ("xlsx-sample-multiple-sheets.xlsx", "Sales", "sales"),
        ("xlsx-sample-multiple-sheets.xlsx", "Expenses", "expenses"),
        ("cost_data.xlsx", "橱柜成本", "cost_data"),
    ])
    def test_real_files(self, src, sheet, expected):
        assert normalize_table_name(src, sheet) == expected

    def test_no_leading_digit(self):
        out = normalize_table_name("2026-report.xlsx", "2026")
        assert out[0].isalpha(), out

    def test_always_safe_charset(self):
        for src, sheet in [("a b.xlsx", "x-y"), ("！！！.xlsx", "中文"), ("x.xlsx", "")]:
            out = normalize_table_name(src, sheet)
            assert out and out[0].isalpha()
            assert all(c.isalnum() or c == "_" for c in out), out

    def test_length_capped(self):
        out = normalize_table_name("a" * 200 + ".xlsx", "")
        assert len(out) <= 48
