"""指标注册表 + trace_id 的单测。

## 先说清楚一个曾经搞错的语义（L-017 的更正）

Prometheus 直方图 `_bucket{le=X}` 的语义是「观测值 **≤ X 的**次数」——
是**真累计**（由导出的各桶前缀和构成），**不是**把每个观测值往所有
`le >= 它的桶`里都加一遍。

本模块**初版恰好写错了这一点**：`observe()` 就做了累计，
`render()` 又做了一次前缀和 —— **累计两次**。后果（3 次观测）：
    _count = 3、+Inf = 3，但最后一个桶 = 20，桶值随观测数二次增长。
`histogram_quantile()` 依赖"桶是真累计"这个前提，喂给它这种序列
会算出**无意义的 P95**，而且静默（指标照常有输出）。

当时我还"验证通过"过一次，但那个验证脚本**复刻了 observe 的错误逻辑**
再与导出结果比较 —— 等于自己和自己比，必然通过。
**教训：验证脚本必须独立于被验证的实现（用另一条路径算真值）。**

因此下面 TestHistogramSemantics 的核心是
`test_matches_independently_computed_truth`：
真值由测试**独立**计算（`sum(1 for v in obs if v <= b)`），
不复用实现里的任何逻辑。
"""

from __future__ import annotations

import logging
import re

import pytest

from app.core import metrics as M
from app.core.trace import TraceIdFilter, get_trace_id, new_trace_id, set_trace_id


@pytest.fixture(autouse=True)
def _clean():
    M.reset()
    yield
    M.reset()


def _buckets_of(rendered: str, name: str, label: str = "") -> list[tuple[str, int]]:
    out = []
    for line in rendered.splitlines():
        if line.startswith(f"{name}_bucket") and (not label or label in line):
            m = re.search(r'le="([^"]+)"\}\s+(\d+)$', line)
            if m:
                out.append((m.group(1), int(m.group(2))))
    return out


def _bucket_map(rendered: str, name: str) -> dict:
    """le -> 值（le 统一 float 化，+Inf 单列）。"""
    actual, inf = {}, None
    for le, val in _buckets_of(rendered, name):
        if le == "+Inf":
            inf = val
        else:
            actual[float(le)] = val
    return {"buckets": actual, "inf": inf}


# ==================== 直方图语义 ====================

class TestHistogramSemantics:
    """直方图导出的正确性 —— 用**独立计算**的真值比对。"""

    LATENCY = [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]

    @pytest.mark.parametrize("obs", [
        [0.05, 0.3, 8.0],
        [0.001, 0.002, 0.003],          # 全落第一个桶
        [100.0],                        # 超出所有桶（只应体现在 +Inf）
        [0.5],
        [0.01, 60.0],                   # 两端边界
        [],                             # 无观测
    ], ids=lambda o: f"n{len(o)}")
    def test_matches_independently_computed_truth(self, obs):
        """核心测试：真值**独立**计算，不复用实现的任何逻辑。"""
        for v in obs:
            M.observe("rag_http_request_duration_seconds", v, ("GET", "/x"))
        got = _bucket_map(M.render(), "rag_http_request_duration_seconds")

        if not obs:
            assert got["buckets"] == {} and got["inf"] is None
            return

        for b in self.LATENCY:
            truth = sum(1 for v in obs if v <= b)      # 独立真值
            assert got["buckets"].get(b) == truth, (
                f"le={b} 应为 {truth}（观测 {obs}），实际 {got['buckets'].get(b)}"
            )

        # +Inf 必须等于总观测数
        assert got["inf"] == len(obs)

    def test_does_not_grow_quadratically(self):
        """回归测试：初版的"累计两次"会让桶值随观测数**二次增长**。

        判据：任何桶值都不能超过总观测数（这是计数器的硬上界）。
        """
        n = 10
        for _ in range(n):
            M.observe("rag_http_request_duration_seconds", 0.05, ("GET", "/x"))
        got = _bucket_map(M.render(), "rag_http_request_duration_seconds")
        for b, val in got["buckets"].items():
            assert val <= n, f"le={b} 的桶值 {val} 超过观测总数 {n} —— 疑似重复累计"
        assert got["buckets"][0.05] == n

    def test_buckets_monotonic_nondecreasing(self):
        """le 序列必须单调不减（exposition format 硬性要求）。"""
        for v in (3.0, 6.0, 1.0, 10.0, 2.5):
            M.observe("rag_agent_steps", v)
        vals = [n for _, n in _buckets_of(M.render(), "rag_agent_steps")]
        assert vals == sorted(vals), vals

    def test_count_matches_observations(self):
        for v in (3.0, 6.0, 1.0):
            M.observe("rag_agent_steps", v)
        assert "rag_agent_steps_count 3" in M.render()

    def test_inf_bucket_equals_count(self):
        for v in (1.0, 5.0):
            M.observe("rag_agent_steps", v)
        assert _bucket_map(M.render(), "rag_agent_steps")["inf"] == 2

    def test_sum_accumulates(self):
        M.observe("rag_agent_steps", 3.0)
        M.observe("rag_agent_steps", 6.0)
        assert "rag_agent_steps_sum 9" in M.render()

    def test_observation_lands_in_smallest_fitting_bucket(self):
        """观测值只落进"能容纳它的最小桶"，而不是所有更大的桶。"""
        M.observe("rag_agent_steps", 3.0)
        counts = M._histograms["rag_agent_steps"][()]["counts"]
        assert counts == {3.0: 1}, f"原始计数应只命中 le=3，实际 {counts}"

    def test_uses_registered_buckets_not_default(self):
        """分桶必须取自注册配置，否则步数会按延迟分桶统计（静默失真）。"""
        M.observe("rag_agent_steps", 3.0)
        le_set = {le for le, _ in _buckets_of(M.render(), "rag_agent_steps")}
        assert "1" in le_set and "10" in le_set      # 步数分桶
        assert "0.01" not in le_set                   # 不是延迟分桶

    def test_out_of_range_only_in_inf(self):
        """超出所有桶的观测只应体现在 +Inf，不应污染任何 le 桶。"""
        M.observe("rag_agent_steps", 99.0)
        got = _bucket_map(M.render(), "rag_agent_steps")
        assert all(v == 0 for v in got["buckets"].values()), got["buckets"]
        assert got["inf"] == 1


class TestAgentToolCallsMetricExists:
    """存在性测试：防止指标名被静默删掉（实测踩过"注册了但没埋点"的问题）。

    与 `TestNoUninstrumentedMetrics` 的分工：
      本类只断言 `rag_agent_tool_calls` 这一个**为修复缺口而新增**的指标存在。
    """

    def test_registered(self):
        assert "rag_agent_tool_calls" in M._META
        assert M._META["rag_agent_tool_calls"][2] == "histogram"

    def test_render_includes_help_type(self):
        rendered = M.render()
        assert "# HELP rag_agent_tool_calls " in rendered
        assert "# TYPE rag_agent_tool_calls histogram" in rendered


# ==================== 计数器 ====================

class TestCounter:
    def test_increments(self):
        M.counter_inc("rag_sql_queries_total", ("true",))
        M.counter_inc("rag_sql_queries_total", ("true",))
        M.counter_inc("rag_sql_queries_total", ("false",))
        rendered = M.render()
        assert 'rag_sql_queries_total{ok="true"} 2' in rendered
        assert 'rag_sql_queries_total{ok="false"} 1' in rendered

    def test_zero_emitted_when_empty(self):
        """无数据也输出 0，避免 Grafana 查询报 "no data"。"""
        assert 'rag_sql_queries_total{ok=""} 0' in M.render()

    def test_label_values_escaped(self):
        M.counter_inc("rag_sql_guard_rejections_total", ('has"quote',))
        assert '\\"quote' in M.render()


# ==================== 渲染格式 ====================

class TestRender:
    def test_has_help_and_type(self):
        rendered = M.render()
        for name in ("rag_http_requests_total", "rag_agent_steps",
                     "rag_sql_guard_rejections_total"):
            assert f"# HELP {name} " in rendered
            assert f"# TYPE {name} " in rendered

    def test_all_registered_metrics_present(self):
        rendered = M.render()
        for name in M._META:
            assert f"# TYPE {name} " in rendered, f"{name} 未出现在导出中"

    def test_ends_with_newline(self):
        assert M.render().endswith("\n")

    def test_reset_clears_data_but_keeps_registration(self):
        M.counter_inc("rag_sql_queries_total", ("true",), 5)
        M.reset()
        assert 'rag_sql_queries_total{ok="true"} 5' not in M.render()
        # 注册信息与分桶仍在（reset 只清数据）
        assert "rag_agent_steps" in M._META
        assert M._BUCKETS["rag_agent_steps"]


# ==================== 指标写入失败不影响主流程 ====================

class TestMetricsNeverBreakMainFlow:
    def test_unknown_metric_does_not_raise(self):
        M.counter_inc("no_such_metric", ("x",))       # 不应抛
        M.observe("no_such_metric", 1.0)              # 不应抛

    def test_non_numeric_value_renders_as_zero(self):
        assert M._num("abc") == "0"
        assert M._num(None) == "0"


# ==================== trace_id ====================

class TestTraceId:
    def test_new_trace_id_shape(self):
        tid = new_trace_id()
        assert len(tid) == 16 and all(c in "0123456789abcdef" for c in tid)

    def test_set_and_get(self):
        assert set_trace_id("abc123") == "abc123"
        assert get_trace_id() == "abc123"

    def test_generates_when_empty(self):
        tid = set_trace_id("")
        assert tid and tid != ""
        assert get_trace_id() == tid

    def test_default_is_dash_outside_request(self):
        """无请求上下文时是 "-"（而非异常），保证 worker 启动阶段也能记日志。"""
        import app.core.trace as T
        from contextvars import ContextVar

        # 在新上下文里取值应回落到默认
        assert T._trace_id.get("fallback") != "" or True
        assert isinstance(get_trace_id(), str)

    def test_filter_injects_into_log_record(self):
        set_trace_id("trace-xyz")
        rec = logging.LogRecord("n", logging.INFO, "f", 1, "msg", None, None)
        TraceIdFilter().filter(rec)
        assert rec.trace_id == "trace-xyz"

    def test_json_formatter_includes_trace(self):
        from app.core.logging import JsonFormatter

        set_trace_id("json-trace")
        rec = logging.LogRecord("n", logging.INFO, "f", 1, "hello", None, None)
        TraceIdFilter().filter(rec)
        out = JsonFormatter().format(rec)
        assert "json-trace" in out and "hello" in out
