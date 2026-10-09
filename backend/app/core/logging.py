"""
日志配置（结构化 + trace_id 贯穿）

两种输出模式，由 LOG_FORMAT 控制：
    text（默认）—— 人读友好单行；容器里 grep/tail 最顺手
    json        —— 结构化单行 JSON；接 ELK/Loki 等采集器时用

两种模式都带 `trace_id`（见 core/trace.py），因此一次请求在
HTTP / Agent / SQL / worker 各处产生的日志可以拼接起来 ——
这是"这个用户的这次请求到底发生了什么"能被回答的前提。

刻意不引入 structlog：标准库 + logging.Filter 已能完整实现，
少一个依赖、构建更快、行为完全可控。
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict

from app.core.trace import TraceIdFilter

_TEXT_FORMAT = "%(asctime)s | %(levelname)-7s | %(trace_id)s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# LogRecord 自带字段（JSON 模式下不当业务字段输出）
_STD_FIELDS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "taskName", "thread", "threadName", "trace_id",
})

_configured = False


class JsonFormatter(logging.Formatter):
    """单行 JSON 格式化器（便于日志采集器解析）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, _DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": getattr(record, "trace_id", "-"),
            "msg": record.getMessage(),
        }
        # 异常堆栈必须保留（排查时最关键的信息）
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # 业务自定义字段（logger.info("...", extra={"k": v})）
        for k, v in record.__dict__.items():
            if k in _STD_FIELDS or k.startswith("_") or k in payload:
                continue
            try:
                json.dumps(v)      # 只收可序列化的
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = str(v)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """初始化根日志器（幂等，重复调用不会叠加 handler）。

    Args:
        level: 日志级别
        fmt: "text"（默认，人读友好）或 "json"（结构化采集）
    """
    global _configured
    if _configured:
        return

    handler = logging.StreamHandler(sys.stdout)
    if str(fmt).lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT, datefmt=_DATE_FORMAT))
    # trace_id 注入必须在 formatter 生效前挂上
    handler.addFilter(TraceIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # 降噪：第三方库默认 WARNING
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
