"""retry.py - 针对外部 API 的瞬时故障重试（指数退避 + 抖动）。

## 为什么需要它（实测驱动，见 docs/经验教训.md L-013）

实测后端日志出现：
    [Embedding] API 请求失败: 504 Server Error: Gateway Time-out
    [tools] kb_search 失败: generator didn't stop after throw()
即 **上游网关偶发 504**，而 embedding 客户端**没有任何重试**，
于是一次抖动就直接把整个问答打成失败（且错误信息还被 contextlib 掩盖成
"generator didn't stop after throw()"，完全误导排查方向）。

对 RAG/Agent 这类**多次外呼**的链路，偶发失败是常态而非异常：
    一次问答 = 1 次 embedding + 1 次 rerank + 1~3 次 LLM
    单次成功率 99% ⇒ 全链路成功率 ≈ 0.99^5 ≈ 95%
加上 Agent 的多步循环，链路更长。**没有重试，可用性会被乘性放大掉。**

## 重试策略

只重试**瞬时性**故障，绝不重试**确定性**错误：
    重试：连接错误 / 超时 / 429 / 5xx
    不重试：4xx（除 429）—— 请求本身有问题，重试只会浪费配额与时间
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Iterable, Optional, Tuple, TypeVar

import requests

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 可重试的 HTTP 状态码
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 可重试的异常类型
RETRYABLE_EXCEPTIONS: Tuple[type, ...] = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def is_retryable(exc: Exception) -> bool:
    """该异常是否值得重试。

    4xx（除 429）不重试：请求参数/鉴权有问题，重试不会变好。
    """
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code in RETRYABLE_STATUS
    return isinstance(exc, RETRYABLE_EXCEPTIONS)


def call_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.6,
    max_delay: float = 6.0,
    label: str = "external_api",
    on_retry: Optional[Callable[[int, Exception, float], None]] = None,
) -> T:
    """执行 fn()，对瞬时故障重试。

    Args:
        attempts: 总尝试次数（含首次）。attempts=1 表示不重试。
        base_delay: 首次退避秒数；之后指数增长。
        max_delay: 单次退避上限。
        label: 日志标识（便于区分是哪个外呼在重试）。
        on_retry: 可选回调 (第几次失败从1开始, 异常, 本次退避秒数)。

    Returns:
        fn() 的返回值。

    Raises:
        最后一次的异常（若全部失败）。**保留原异常类型**，
        便于上层按类型区分处理（例如 rerank 失败要降级、embedding 失败要报错）。
    """
    attempts = max(1, int(attempts))
    last_exc: Optional[Exception] = None

    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            retryable = is_retryable(e)
            if i >= attempts or not retryable:
                if retryable:
                    logger.error("[retry] %s 重试 %d 次后仍失败: %s", label, attempts, str(e)[:200])
                else:
                    logger.error("[retry] %s 不可重试的错误（直接抛出）: %s", label, str(e)[:200])
                raise

            # 指数退避 + 抖动（抖动避免多个请求同时重试造成"重试风暴"）
            delay = min(max_delay, base_delay * (2 ** (i - 1)))
            delay += random.uniform(0, delay * 0.25)
            logger.warning(
                "[retry] %s 第 %d/%d 次失败（%s），%.2fs 后重试: %s",
                label, i, attempts, type(e).__name__, delay, str(e)[:150],
            )
            if on_retry:
                try:
                    on_retry(i, e, delay)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(delay)

    # 理论不可达（循环内要么 return 要么 raise）
    raise last_exc if last_exc else RuntimeError(f"{label} 未知失败")
