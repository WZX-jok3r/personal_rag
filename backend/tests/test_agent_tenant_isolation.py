"""Agent 检索路径的租户隔离测试。

## 本文件针对的真实跨租户泄露（双租户 E2E 实测发现）

经典 RAG 路径有隔离：`api/deps.enforced_filter()` 用 Principal 强制叠加租户键，
实测 `hidden_count=4`（租户 b 的向量被挡住）。

但 **Agent 路径绕过了那个依赖**：`AgentTools.dispatch` 调 `kb_search` 时
**没有传 tenant_id**，于是 `filter_dict=None` ⇒ Retriever 不做任何过滤
⇒ **Agent 能看到所有租户的私有向量**。

实测证据：租户 c 通过 `/agent/stream` 提问，拿到了租户 b 私有上传
的 xlsx 内容与金额（"B项目一 111 / B项目二 222"）。

根因是**两条路径的隔离机制不对称** —— 一个靠 FastAPI 依赖注入，
另一个自己拼 filter，于是后者漏了。这类不对称缺陷极难靠单路径测试发现，
所以下面既测"Agent 路径有隔离"，也测"不能再出现绕过"。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.agent.tools import AgentTools
from app.core.config import settings


class RecordingRetriever:
    """记录每次 search 收到的 filter_dict，供断言隔离是否注入。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def search(self, query: str, top_k: int = 5,
               filter_dict: Optional[Dict[str, Any]] = None):
        self.calls.append({"query": query, "top_k": top_k, "filter": filter_dict})
        return [], 0


@pytest.fixture
def tools():
    r = RecordingRetriever()
    t = AgentTools(retriever=r)
    t._recording = r          # 便于测试取用
    return t


TF = settings.tenant_field


class TestKbSearchEnforcesTenant:
    """kb_search 必须把租户条件注入到 Retriever 的 filter。"""

    def test_tenant_is_injected(self, tools):
        tools.kb_search("问题", tenant_id="tenant-b")
        f = tools._recording.calls[0]["filter"]
        assert f is not None, "kb_search 没有注入任何 filter —— 跨租户泄露"
        assert f.get(TF) == "tenant-b"

    def test_no_tenant_means_no_filter(self, tools):
        """无租户（鉴权关闭的本地/评测场景）时不注入 —— 与既有软关闭语义一致。"""
        tools.kb_search("问题", tenant_id=None)
        assert tools._recording.calls[0]["filter"] is None

    def test_caller_supplied_tenant_is_ignored(self, tools):
        """调用方传入的租户键必须被丢弃（防止模型/客户端伪造越权）。"""
        tools.kb_search("问题", tenant_id="tenant-b",
                        filter_dict={TF: "tenant-a", "format": "xlsx"})
        f = tools._recording.calls[0]["filter"]
        assert f[TF] == "tenant-b", "调用方传的租户键覆盖了 principal，存在越权"
        assert f["format"] == "xlsx", "普通业务过滤条件不应被丢弃"

    def test_empty_tenant_string_still_filtered(self, tools):
        """空串也是有效值（fail-closed 下匹配不到任何租户），不能被当成 None。"""
        tools.kb_search("问题", tenant_id="")
        assert tools._recording.calls[0]["filter"] == {TF: ""}


class TestDispatchPropagatesTenant:
    """dispatch 必须把 tenant_id 传给 kb_search（漏传就是那个泄露根因）。"""

    def test_dispatch_passes_tenant(self, tools):
        tools.dispatch("kb_search", {"query": "问题"}, tenant_id="tenant-b")
        f = tools._recording.calls[0]["filter"]
        assert f is not None and f.get(TF) == "tenant-b", (
            "dispatch 没把 tenant_id 传给 kb_search —— 这正是跨租户泄露的根因"
        )

    def test_dispatch_argument_name_is_tenant_id(self):
        """签名断言：防止有人把参数改名/删掉导致静默失去隔离。"""
        import inspect

        sig = inspect.signature(AgentTools.dispatch)
        assert "tenant_id" in sig.parameters

    def test_kb_search_signature_accepts_tenant(self):
        import inspect

        sig = inspect.signature(AgentTools.kb_search)
        assert "tenant_id" in sig.parameters, (
            "kb_search 又不接受 tenant_id 了 —— 隔离会静默失效"
        )


class TestNoPathBypassesTenantFilter:
    """结构性断言：不允许再出现"另一条检索路径没接租户"的情况。

    这条测试的价值在于**防止同类缺陷复发**：
    本次泄露的本质是"两条路径机制不对称"，而不是某一行写错。
    """

    def test_only_two_retrieval_entrypoints(self):
        """穷举代码库里所有 `retriever.search(` 调用点，必须都在受控路径内。"""
        import re
        from pathlib import Path

        app_dir = Path(__file__).resolve().parent.parent / "app"
        allowed = {
            # Agent 工具层：本文件已测其强制注入
            "agent/tools.py",
            # 经典 RAG pipeline：由 api/deps.enforced_filter 注入
            "rag/pipeline.py",
        }
        offenders = []
        for path in app_dir.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if re.search(r"retriever\.search\s*\(", text):
                rel = path.relative_to(app_dir).as_posix()
                if rel not in allowed:
                    offenders.append(rel)
        assert not offenders, (
            f"发现未受控的检索调用点 {offenders} —— "
            f"新路径必须显式处理租户隔离，否则会重演跨租户泄露"
        )

    def test_pipeline_receives_enforced_filter(self):
        """经典路径必须经 enforced_filter 注入（回归保护）。"""
        from app.api.deps import enforced_filter
        from app.core.security import Principal

        p = Principal(tenant_id="tenant-c", authenticated=True)
        f = enforced_filter({TF: "tenant-a"}, p)
        assert f[TF] == "tenant-c", "enforced_filter 未以 principal 为准"
