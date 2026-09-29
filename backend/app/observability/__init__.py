"""可观测层（Langfuse 埋点）。

对外统一从包导入：``from app.observability import langfuse as obs``，
或 ``from app.observability import span, generation, trace``。
"""

from app.observability.langfuse import (
    enabled,
    extract_usage,
    flush,
    generation,
    span,
    trace,
)

__all__ = ["enabled", "extract_usage", "flush", "generation", "span", "trace"]
