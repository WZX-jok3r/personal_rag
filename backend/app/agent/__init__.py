"""agent - 内部知识库 Agent 编排层。

组成：
    router.py  : 三层混合路由的第一层（规则前置，纯函数可穷举单测）
    tools.py   : 工具定义与执行（kb_search / sql_query / list_data_tables）
    loop.py    : 有界状态机 Agent Loop（防死循环五重保护）
    events.py  : SSE 事件契约（扩帧不改帧，向后兼容）

设计立场（见改造方案 4.5 / 5.4）：**手写约 300 行有界状态机，不引入 Agent 框架。**
理由：只有 3 个工具、循环上界明确、无多 Agent 协作、无人在回路审批；
且必须复用既有 SSE 契约与 hermetic 测试体系。
框架会把这些"包起来"，反而增加调试链路长度。
切换边界：需要多 Agent 协作或 HITL 审批时切 LangGraph；
需要复杂结构化输出时优先 Pydantic AI（与本项目 Pydantic v2 + FastAPI 栈最契合）。
"""

__all__ = ["router", "tools", "loop", "events"]
