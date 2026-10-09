"""指标埋点覆盖性检查：**每个注册的指标都必须真的有埋点**。

## 为什么需要这个测试（由真实缺陷驱动）

出过一个真实问题：`rag_sql_guard_rejections_total` 等指标在 `metrics.py` 里
注册了（于是 `/metrics` 会输出 `# HELP` / `# TYPE`），但**代码里没有任何地方
调用 `counter_inc`** —— 于是它在报表上恒为 0。

这类缺陷的可怕之处：
  - **不报错**：指标有输出，"看起来配好了"；
  - **误导判断**：0 会被解读成"线上没有攻击/没有降级"，
    而真相是"根本没在数"；
  - 排查方向错：会去查网关/规则，而不是查埋点。

`# TYPE` 行只能证明"注册了"，**不能证明"在埋点"**。
因此这里做静态扫描：在所有业务代码里搜索指标名，
确认它**出现在 metrics.py 之外**（即真的被某个调用点引用）。

## 这个测试的局限（诚实说明）

静态扫描只能证明"名字被引用过"，不能证明"在正确的时机被调用"。
真正的行为验证由各指标自己的集成测试负责（例如
`tests/test_agent_loop.py::TestObservability`）。
两者互补：本测试防"完全没埋"，那些测试防"埋错地方"。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

import pytest

from app.core import metrics as M

APP_DIR = Path(__file__).resolve().parent.parent / "app"
METRICS_FILE = APP_DIR / "core" / "metrics.py"

# 允许"注册但暂未埋点"的白名单。
# 为空表示：所有注册的指标都必须有埋点。
# 若确实需要先注册后埋点，请在此显式登记并说明原因 ——
# 让"未埋点"成为**有意识的决定**，而不是疏忽。
ALLOWLIST: Dict[str, str] = {}


def _instrumented_names() -> Dict[str, List[str]]:
    """扫描 app/ 下所有 .py（除 metrics.py），返回 指标名 -> [引用它的文件]。"""
    found: Dict[str, List[str]] = {}
    for path in APP_DIR.rglob("*.py"):
        if path == METRICS_FILE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name in M._META:
            # 精确匹配带引号的指标名，避免子串误命中
            if re.search(rf'["\']{re.escape(name)}["\']', text):
                found.setdefault(name, []).append(
                    str(path.relative_to(APP_DIR.parent))
                )
    return found


class TestNoUninstrumentedMetrics:
    """所有注册的指标都必须被埋点。"""

    def test_every_registered_metric_is_instrumented(self):
        used = _instrumented_names()
        missing = [
            name for name in M._META
            if name not in used and name not in ALLOWLIST
        ]
        assert not missing, (
            "以下指标已注册（会在 /metrics 输出 HELP/TYPE）但**从未被埋点**，"
            "报表上会恒为 0 而被误读为『线上没发生』：\n  - "
            + "\n  - ".join(missing)
        )

    def test_allowlist_entries_still_exist(self):
        """白名单里的指标若已被删除或已埋点，应清理白名单，避免腐化。"""
        used = _instrumented_names()
        stale = [n for n in ALLOWLIST if n not in M._META or n in used]
        assert not stale, f"白名单已过期（应移除）: {stale}"


class TestHighValueMetricsAreCovered:
    """点名验证那几个"安全/可用性"价值最高的指标确实有埋点。"""

    @pytest.mark.parametrize("name", [
        "rag_sql_guard_rejections_total",   # 安全网关真实拦截量
        "rag_sql_redactions_total",         # 敏感数据访问尝试
        "rag_agent_runs_total",             # 通道占比 + 降级率
        "rag_agent_tool_calls",             # 真实工作量
        "rag_llm_tokens_total",             # 成本
        "rag_sql_queries_total",            # SQL 成败
        "rag_retrieval_chunks",             # 召回条数
        "rag_http_requests_total",          # QPS/错误率（中间件埋点）
    ])
    def test_metric_has_call_site(self, name):
        used = _instrumented_names()
        assert name in used, f"{name} 没有埋点，报表会恒为 0"
        assert used[name], f"{name} 的引用文件列表为空"


class TestMetricsModuleItself:
    """metrics.py 只应定义与导出，不应把指标名"用"在业务逻辑里。"""

    def test_meta_and_buckets_consistent(self):
        """histogram 必须有分桶配置，counter 不应有。"""
        for name, (_, _, mtype) in M._META.items():
            if mtype == "histogram":
                assert name in M._BUCKETS, f"{name} 是 histogram 但无分桶配置"
            else:
                assert name not in M._BUCKETS, f"{name} 不是 histogram 却配了分桶"

    def test_all_metrics_are_counter_or_histogram(self):
        for name, (_, _, mtype) in M._META.items():
            assert mtype in ("counter", "histogram"), f"{name} 类型非法: {mtype}"
