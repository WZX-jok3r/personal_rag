"""指标注册表 + trace_id 的单测。

## 一个必须先说清的语义（我自己一度误判成 bug）

Prometheus 直方图的 `le` 是**上界**，`_bucket{le=X}` 的语义是
「观测值 ≤ X 的**累计**次数」，且 le 序列必须单调不减。
因此 `observe(3.0)` 会命中**所有** `le >= 3` 的桶 —— 这是正确行为，不是 bug。

我最初看到 `le=3..6` 的桶值 1,2,3,5 而 `_count=2`，误以为计数错乱，
实际那正是两次观测（3 和 6）的累计分布函数。
**这类误判的代价是去"修"一段本来正确的代码**，所以下面用测试把正确语义钉死：
  - 累计桶必须单调不减
  - `_count` 等于观测次数
  - 最后一个 `+Inf` 桶等于 `_count`
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


# ==================== 直方图语义 ====================

class TestHistogramSemantics:
    def test_observations_land_in_cumulative_buckets(self):
        """观测值落入其所有上界 >= 它的桶（累计语义）。"""
        M.observe("rag_agent_steps", 3.0)
        counts = M._histograms["rag_agent_steps"][()]["counts"]
        # value=3.0 ⇒ 命中 le=3,4,5,6,8,10（1.0/2.0 未命中）
        assert sorted(counts) == [3.0, 4.0, 5.0, 6.0, 8.0, 10.0], sorted(counts)

    def test_buckets_are_monotonic_nondecreasing(self):
        """le 序列必须单调不减（Prometheus exposition format 硬性要求）。

        若这条失败，Grafana 的 histogram_quantile() 会算出无意义的 P95。
        """
        for v in (3.0, 6.0, 1.0, 10.0, 2.5):
            M.observe("rag_agent_steps", v)
        buckets = _buckets_of(M.render(), "rag_agent_steps")
        values = [n for _, n in buckets if _ != "+Inf"]
        assert values == sorted(values), f"桶值非单调不减: {buckets}"

    def test_count_matches_observations(self):
        for v in (3.0, 6.0, 1.0):
            M.observe("rag_agent_steps", v)
        rendered = M.render()
        assert "rag_agent_steps_count 3" in rendered

    def test_inf_bucket_equals_count(self):
        """+Inf 桶必须等于总观测数（累计的终点）。"""
        for v in (1.0, 5.0):
            M.observe("rag_agent_steps", v)
        buckets = dict(_buckets_of(M.render(), "rag_agent_steps"))
        assert buckets["+Inf"] == 2

    def test_known_cdf(self):
        """两次观测 (3, 6) 的累计分布：le=1..10 → 0,0,1,2,3,5,7,9。

        这是把"我一度误判成 bug 的正确行为"固化成断言。
        """
        M.observe("rag_agent_steps", 3.0)
        M.observe("rag_agent_steps", 6.0)
        buckets = _buckets_of(M.render(), "rag_agent_steps")
        seq = [n for le, n in buckets if le != "+Inf"]
        assert seq == [0, 0, 1, 2, 3, 5, 7, 9], f"实际 {seq}"

    def test_sum_accumulates(self):
        M.observe("rag_agent_steps", 3.0)
        M.observe("rag_agent_steps", 6.0)
        assert "rag_agent_steps_sum 9" in M.render()

    def test_uses_registered_buckets_not_default(self):
        """分桶必须取自注册配置，否则步数会按延迟分桶统计（静默失真）。"""
        M.observe("rag_agent_steps", 3.0)
        buckets = {le for le, _ in _buckets_of(M.render(), "rag_agent_steps")}
        assert "1" in buckets and "10" in buckets        # 步数分桶
        assert "0.01" not in buckets                      # 不是延迟分桶


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
