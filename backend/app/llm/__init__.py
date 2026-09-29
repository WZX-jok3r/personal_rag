"""LLM 网关：对上层屏蔽具体 provider（当前 SiliconFlow / OpenAI 兼容）。"""

from app.llm.provider import LLMClient, get_llm_client

__all__ = ["LLMClient", "get_llm_client"]
