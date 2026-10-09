"""trace.py - trace_id 贯穿（HTTP -> Agent -> SQL -> worker）。

## 为什么需要它

没有 trace_id 时，一次问答在日志里是这样的：
    12:00:01 | INFO | app.api.v1.agent | [agent] 路由=sql
    12:00:01 | INFO | app.vector.qdrant  | [Search] 查询 "..." 返回 4 条
    12:00:02 | WARNING | app.core.retry | [retry] embedding 第 1/3 次失败
    12:00:03 | WARNING | app.executor    | SQL 执行失败
**这四行可能来自四个不同的并发请求，无法拼接。**
线上排查时最常问的两个问题是"这个用户的这次请求发生了什么"
以及"这个错误影响了多少请求" —— 没有 trace_id 两个都回答不了。

## 实现（刻意不引入第三方库）

用标准库即可完整实现：
- `contextvars.ContextVar` 存当前 trace_id（**async-safe**：每个请求任务独立，
  不会像全局变量那样在并发请求间串号）
- `logging.Filter` 把它注入每条日志记录
- 一个 middleware 负责生成/透传（读 `X-Request-ID`，没有则生成）
- 响应头回传 `X-Request-ID`，便于前端报错时附上、直接对日志

选 stdlib 而非 structlog 的理由：少一个依赖、构建更快、行为完全可控；
本项目日志量级用不上 structlog 的结构化能力。
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Optional

# 当前请求的 trace_id。默认 "-" 表示"不在请求上下文中"（如 worker 启动阶段）。
_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")

TRACE_HEADER = "X-Request-ID"


def new_trace_id() -> str:
    """生成一个短 trace_id（16 位十六进制，够用且不喧宾夺主）。"""
    return uuid.uuid4().hex[:16]


def set_trace_id(trace_id: Optional[str] = None) -> str:
    """设置当前上下文的 trace_id；不传则生成新的。返回最终使用的值。"""
    tid = (trace_id or "").strip() or new_trace_id()
    _trace_id.set(tid)
    return tid


def get_trace_id() -> str:
    return _trace_id.get()


def bind_trace(trace_id: str) -> str:
    """显式绑定（worker 任务用：从任务参数里带过来，与入队时的 trace 串起来）。"""
    _trace_id.set(trace_id or "-")
    return _trace_id.get()


class TraceIdFilter(logging.Filter):
    """把 trace_id 注入每条日志记录，供 formatter 用 `%(trace_id)s` 引用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id.get()
        return True


class TraceIdMiddleware:
    """ASGI 中间件：为每个 HTTP 请求建立 trace 上下文并回传响应头。

    刻意写成**纯 ASGI 中间件**而不是 Starlette BaseHTTPMiddleware：
    后者会包一层 anyio 任务组，在 SSE 流式场景下可能引入缓冲/取消语义问题
    （本项目 SSE 是核心链路，不能冒险）。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 透传上游（nginx / 网关）的 request id，便于跨服务串联
        incoming = ""
        for k, v in scope.get("headers") or []:
            if k.decode("latin-1").lower() == TRACE_HEADER.lower():
                incoming = v.decode("latin-1")
                break

        tid = set_trace_id(incoming)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                # 回传给前端：报错时用户可提供它，直接定位到日志
                headers.append((TRACE_HEADER.encode("latin-1"), tid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)
