"""重试工具与异常链解包的单测。

背景（docs/经验教训.md L-013）：实测上游 embedding 网关偶发 504，
而客户端零重试，一次抖动就把整个问答打成失败；
更糟的是最终错误信息被 contextlib 包装成
"generator didn't stop after throw()"，把排查引向完全错误的方向。
"""

from __future__ import annotations

import requests
import pytest

from app.agent.tools import describe_exception
from app.core.retry import call_with_retry, is_retryable


def _http_error(status: int) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.exceptions.HTTPError(f"{status} error", response=resp)


class TestIsRetryable:
    """只重试瞬时故障，绝不重试确定性错误。"""

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_retryable_status(self, status):
        assert is_retryable(_http_error(status))

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_client_errors_not_retryable(self, status):
        """4xx 是请求本身有问题，重试只会浪费配额与时间。"""
        assert not is_retryable(_http_error(status))

    def test_timeout_retryable(self):
        assert is_retryable(requests.exceptions.Timeout("timeout"))

    def test_connection_error_retryable(self):
        assert is_retryable(requests.exceptions.ConnectionError("refused"))

    def test_generic_exception_not_retryable(self):
        assert not is_retryable(ValueError("bad input"))


class TestCallWithRetry:
    def test_success_first_try(self):
        calls = []

        def fn():
            calls.append(1)
            return "ok"

        assert call_with_retry(fn, attempts=3, base_delay=0.01) == "ok"
        assert len(calls) == 1, "成功时不应重试"

    def test_retries_then_succeeds(self):
        """瞬时故障后恢复：应重试并最终成功（这是 504 抖动的实际场景）。"""
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise _http_error(504)
            return "recovered"

        assert call_with_retry(fn, attempts=3, base_delay=0.01) == "recovered"
        assert len(calls) == 3

    def test_exhausts_attempts_and_raises(self):
        calls = []

        def fn():
            calls.append(1)
            raise _http_error(503)

        with pytest.raises(requests.exceptions.HTTPError):
            call_with_retry(fn, attempts=3, base_delay=0.01)
        assert len(calls) == 3, f"应尝试 3 次，实际 {len(calls)}"

    def test_does_not_retry_client_error(self):
        """4xx 应立即抛出，不做无谓重试。"""
        calls = []

        def fn():
            calls.append(1)
            raise _http_error(401)

        with pytest.raises(requests.exceptions.HTTPError):
            call_with_retry(fn, attempts=3, base_delay=0.01)
        assert len(calls) == 1, f"401 不应重试，实际尝试 {len(calls)} 次"

    def test_preserves_exception_type(self):
        """必须保留原异常类型：上层要靠类型区分"降级"还是"报错"。"""
        def fn():
            raise requests.exceptions.Timeout("slow")

        with pytest.raises(requests.exceptions.Timeout):
            call_with_retry(fn, attempts=2, base_delay=0.01)

    def test_attempts_one_means_no_retry(self):
        calls = []

        def fn():
            calls.append(1)
            raise _http_error(500)

        with pytest.raises(requests.exceptions.HTTPError):
            call_with_retry(fn, attempts=1, base_delay=0.01)
        assert len(calls) == 1

    def test_on_retry_callback_invoked(self):
        seen = []

        def fn():
            raise _http_error(502)

        with pytest.raises(requests.exceptions.HTTPError):
            call_with_retry(fn, attempts=2, base_delay=0.01,
                            on_retry=lambda i, e, d: seen.append(i))
        assert seen == [1]

    def test_backoff_is_bounded(self):
        """退避必须封顶，避免雪崩式等待。"""
        import time
        t0 = time.perf_counter()

        def fn():
            raise _http_error(503)

        with pytest.raises(requests.exceptions.HTTPError):
            call_with_retry(fn, attempts=3, base_delay=0.01, max_delay=0.02)
        # 2 次退避，每次 <= ~0.025s，总耗时应远小于 1s
        assert time.perf_counter() - t0 < 1.0


class TestDescribeException:
    """异常链解包：把真正的根因从包装异常里挖出来。"""

    def test_single_exception(self):
        out = describe_exception(ValueError("bad"))
        assert "ValueError" in out and "bad" in out

    def test_unwraps_to_root_cause(self):
        """复现实测场景：真实 504 被 RuntimeError 包装。

        若只报最外层，排查会被引向"生成器/线程池"这个完全错误的方向。
        """
        root = _http_error(504)
        try:
            try:
                raise root
            except requests.exceptions.HTTPError as e:
                raise RuntimeError("generator didn't stop after throw()") from e
        except RuntimeError as outer:
            out = describe_exception(outer)

        assert "504" in out, f"必须暴露根因 504，实际: {out}"
        assert "HTTPError" in out
        assert "generator didn't stop" in out, "异常链信息也应保留（便于对照）"

    def test_handles_implicit_context(self):
        """__context__（隐式链）也要能解开，不只是 __cause__。"""
        try:
            try:
                raise _http_error(503)
            except requests.exceptions.HTTPError:
                raise ValueError("wrapped")
        except ValueError as e:
            out = describe_exception(e)
        assert "503" in out

    def test_no_infinite_loop_on_self_reference(self):
        e = ValueError("self")
        e.__context__ = e  # 人为自引用
        out = describe_exception(e)
        assert "self" in out

    def test_truncates_long_messages(self):
        out = describe_exception(ValueError("x" * 500))
        assert len(out) < 400
