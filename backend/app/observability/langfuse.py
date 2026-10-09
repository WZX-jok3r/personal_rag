"""
langfuse.py - 可观测层（Langfuse），软依赖

设计要点:
- 未安装 langfuse 或未配置密钥(settings.langfuse_enabled=False)时, trace/span/generation
  全部返回 no-op 句柄, 对主链路零影响、零网络调用, 行为与未接入可观测完全一致。
- 启用后: 一次 query = 一个 trace(根 span); 内部 retrieve / llm / embedding / rerank
  作为嵌套 observation(SDK 通过上下文自动挂接父子); LLM generation 记录 token usage
  与模型, 便于统计成本/延迟/命中链路。
- 项目用 httpx/requests 手写调用 LLM/Embedding/Rerank(非 LangChain), 故用 Langfuse
  低层 API(start_as_current_span / start_as_current_generation)手动埋点, 而非 CallbackHandler。
- 所有对底层 SDK 的调用都包 try/except 并做 hasattr 兼容, 避免 SDK 版本差异拖垮主流程。

用法:
    from app.observability import langfuse as obs
    with obs.trace("rag_query", input={"query": q}) as t:
        with obs.span("retrieve", input=q) as sp:
            chunks = retrieve(...)
            sp.update(output={"retrieved_count": len(chunks)})
        with obs.generation("llm", model=self.model, input=q) as g:
            answer, usage = call_llm(...)
            g.update(output=answer, usage=usage)
"""

import logging
from contextlib import contextmanager
from typing import Any, Dict, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# 软依赖：langfuse 未安装时不影响导入
try:
    from langfuse import Langfuse  # v3/v4 SDK
    _LANGFUSE_AVAILABLE = True
except Exception:  # ImportError 或其它导入期异常都降级
    Langfuse = None
    _LANGFUSE_AVAILABLE = False

_client = None
_client_failed = False


def enabled() -> bool:
    """可观测是否真正启用（配置了密钥且安装了 SDK）。"""
    return bool(settings.langfuse_enabled and _LANGFUSE_AVAILABLE)


def _get_client():
    """懒加载 Langfuse 客户端；未启用或初始化失败返回 None（调用方据此走 no-op）。"""
    global _client, _client_failed
    if not enabled() or _client_failed:
        return None
    if _client is None:
        try:
            _client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            )
            logger.info(f"[Langfuse] 可观测已启用, host={settings.langfuse_host}")
        except Exception as e:
            logger.warning(f"[Langfuse] 客户端初始化失败, 降级为 no-op: {e}")
            _client_failed = True
            return None
    return _client


def flush(timeout: Optional[int] = 5):
    """把缓冲区里的 observation 同步导出到 Langfuse（长驻服务/脚本结束前调用，避免丢数据）。"""
    c = _get_client()
    if c is None:
        return
    try:
        c.flush()
    except Exception as e:
        logger.warning(f"[Langfuse] flush 失败(忽略): {e}")


def extract_usage(data: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """
    从 SiliconFlow / OpenAI 风格的响应里抽取 token usage，缺字段安全返回 None。
    返回: {"input": prompt_tokens, "output": completion_tokens, "total": total_tokens}
    """
    usage = (data or {}).get("usage") or {}
    return {
        "input": usage.get("prompt_tokens"),
        "output": usage.get("completion_tokens"),
        "total": usage.get("total_tokens"),
    }


class _NoopHandle:
    """未启用时返回的空句柄：所有方法都是无害 no-op，保持调用点写法统一。"""

    def update(self, *args, **kwargs):
        return self

    def end(self, *args, **kwargs):
        return self


class _ObsHandle:
    """包装真实 Langfuse observation，把 update 转发到底层并吞掉版本差异异常。"""

    def __init__(self, inner):
        self._inner = inner

    def update(self, output=None, input=None, metadata=None, usage=None,
               level=None, status_message=None, **kwargs):
        if self._inner is None:
            return self
        payload: Dict[str, Any] = {}
        if output is not None:
            payload["output"] = output
        if input is not None:
            payload["input"] = input
        if metadata is not None:
            payload["metadata"] = metadata
        if level is not None:
            payload["level"] = level
        if status_message is not None:
            payload["status_message"] = status_message
        payload.update(kwargs)
        try:
            if usage is not None:
                # Langfuse v4 用 usage_details 记录 token; v3 回退用 usage
                try:
                    self._inner.update(usage_details=usage, **payload)
                except TypeError:
                    self._inner.update(usage=usage, **payload)
            else:
                self._inner.update(**payload)
        except Exception as e:
            logger.debug(f"[Langfuse] update 失败(忽略): {e}")
        return self


def _open_obs(client, as_type: str, name: str, input: Any, metadata: Any,
              model: Optional[str] = None):
    """
    适配不同 Langfuse SDK 版本的开启方式，返回一个可作为 with 上下文的管理器：
    - v4: 统一入口 start_as_current_observation(as_type=...)
    - v3: 回退到 start_as_current_generation / start_as_current_span
    """
    if hasattr(client, "start_as_current_observation"):
        kw: Dict[str, Any] = dict(name=name, as_type=as_type, input=input, metadata=metadata)
        if model:
            kw["model"] = model
        return client.start_as_current_observation(**kw)
    if as_type == "generation" and hasattr(client, "start_as_current_generation"):
        return client.start_as_current_generation(name=name, model=model, input=input, metadata=metadata)
    return client.start_as_current_span(name=name, input=input, metadata=metadata)


@contextmanager
def trace(name: str, input: Any = None, metadata: Any = None):
    """开启一次查询的根 observation（trace）。未启用则 yield 空句柄。"""
    c = _get_client()
    if c is None:
        yield _NoopHandle()
        return
    try:
        # 根 span 即构成一次 trace；内部 span/generation 通过上下文自动挂为子 observation
        with _open_obs(c, "span", name, input, metadata) as root:
            yield _ObsHandle(root)
    except Exception as e:
        logger.debug(f"[Langfuse] trace 建立失败(忽略): {e}")
        yield _NoopHandle()


@contextmanager
def span(name: str, input: Any = None, metadata: Any = None):
    """在当前 trace 下开启一个子 observation（如 retrieve / embedding / rerank）。"""
    c = _get_client()
    if c is None:
        yield _NoopHandle()
        return
    try:
        with _open_obs(c, "span", name, input, metadata) as s:
            yield _ObsHandle(s)
    except Exception as e:
        logger.debug(f"[Langfuse] span 建立失败(忽略): {e}")
        yield _NoopHandle()


@contextmanager
def generation(name: str, model: Optional[str] = None, input: Any = None,
               metadata: Any = None):
    """记录一次 LLM 生成，可通过 handle.update(usage=...) 落 token 用量。"""
    c = _get_client()
    if c is None:
        yield _NoopHandle()
        return
    try:
        with _open_obs(c, "generation", name, input, metadata, model=model) as g:
            yield _ObsHandle(g)
    except Exception as e:
        logger.debug(f"[Langfuse] generation 建立失败(忽略): {e}")
        yield _NoopHandle()
