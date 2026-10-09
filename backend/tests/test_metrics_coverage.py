"""指标埋点覆盖性检查：**每个注册的指标都必须真的有埋点**。

## 为什么需要这个测试

`GET /metrics` 会为**每个已注册**的指标输出 `# HELP` / `# TYPE` 行 ——
即使该指标从未被 `counter_inc` / `observe` 调用过。
也就是说 **`# TYPE` 只能证明"注册了"，不能证明"在埋点"**。

一个"注册了但从未埋点"的指标会带来三类危害：
  - **不报错**：报表上有它，"看起来配好了"；
  - **误导判断**：恒为 0 会被解读成"线上没有攻击/没有降级"，
    而真相是"根本没在数"；
  - **排查方向错**：会去查网关/规则，而不是查埋点。

因此这里做静态扫描：在所有业务代码里搜索指标名，
确认它**出现在 metrics.py 之外**（即真的被某个调用点引用）。

## 诚实说明：这不是由"已发生的故障"驱动的

本测试的诞生背景需要说清，避免留下不实陈述：
在一次复审中，有人指出若干指标"根本没埋点、会显示为 0"。
**实测结论是：被点名的大部分指标其实埋点正常**
（`rag_sql_guard_rejections_total`、`rag_agent_runs_total` 都实测有数据），
当时看到的 0 是"空计数器的零值占位行"，属观测方式造成的误读。

但其中**确实暴露了一个真实的度量缺口**：
`rag_agent_steps` 只在 `_run_llm_loop` 里自增，
而**规则强路由路径不进那个循环** —— 于是那条路径的 steps 恒为 0，
可它明明做了实事（为此新增了 `rag_agent_tool_calls` 指标）。

所以本测试的价值是**前瞻性防护**而非"修复已发生的 bug"：
它把"注册了但没埋点"从"只能靠人眼比对"变成"提交即失败"。
这条防线目前是**未被触发过的**（全部指标都有埋点）——
它拦的是**将来**新加指标时忘了埋点的情况。

## 这个测试的局限

静态扫描只能证明"名字被引用过"，不能证明"在正确的时机被调用"，
也不能证明"度量的是对的对象"（上面 `rag_agent_steps` 的语义缺口，
静态扫描就查不出来）。
真正的行为验证由各指标自己的集成测试负责（例如
`tests/test_agent_loop.py`）。
两者互补：本测试防"完全没埋"，那些测试防"埋错地方/量错对象"。
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
