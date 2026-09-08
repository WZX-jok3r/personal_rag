"""
rag_pipeline.py - RAG 完整链路

职责:
1. 接收用户 query
2. 向量检索召回相关 chunk
3. 组装 RAG Prompt（system prompt + context + user query）
4. 调用 SiliconFlow API（DeepSeek-V3.2）生成回答
5. 返回答案 + 引用来源

用法:
    from rag_pipeline import RAGPipeline
    pipeline = RAGPipeline()
    result = pipeline.query("路由器质保多久？")
    print(result["answer"])
    print(result["sources"])
"""

import json
import logging
from typing import List, Dict, Any, Optional

import requests

from config import (
    SILICONFLOW_API_KEY,
    SILICONFLOW_BASE_URL,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
    TOP_K,
)
from vector_store import get_vector_store, VectorStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class RAGPipeline:
    """RAG 问答管道"""

    # RAG System Prompt 模板
    SYSTEM_PROMPT = """你是一个专业的知识库问答助手。请严格根据以下提供的参考资料回答问题。
    规则：
    1. 只使用提供的参考资料作答，不要编造信息
    2. 如果参考资料不足以回答问题，请明确说明"根据现有资料无法回答"
    3. 回答时尽量引用参考资料中的具体信息
    4. 如果多个参考资料有冲突，请指出冲突并说明依据
    
    参考资料：
    {context}
    """

    def __init__(self):
        self.vector_store = get_vector_store()
        self.api_key = SILICONFLOW_API_KEY
        self.base_url = SILICONFLOW_BASE_URL.rstrip("/")
        self.model = LLM_MODEL
        self.temperature = LLM_TEMPERATURE
        self.max_tokens = LLM_MAX_TOKENS

        if not self.api_key:
            raise ValueError("SILICONFLOW_API_KEY 未配置")

    def _retrieve(self, query: str, top_k: int = TOP_K, filter_dict: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """检索相关 chunk"""
        logger.info(f"[Retrieve] 查询: {query}")
        results = self.vector_store.search(query, top_k=top_k, filter_dict=filter_dict)
        logger.info(f"[Retrieve] 召回 {len(results)} 条结果")
        return results

    def _build_context(self, chunks: List[Dict[str, Any]]) -> str:
        """将检索结果组装成 context 文本"""
        context_parts = []
        for i, chunk in enumerate(chunks, 1):
            text = chunk["text"]
            meta = chunk["metadata"]
            source = meta.get("source", "未知来源")
            fmt = meta.get("format", "未知格式")

            # 提取文件名
            from pathlib import Path
            source_name = Path(source).name if source else "未知"

            # 附加页码/行号信息
            extra_info = ""
            if "page_number" in meta:
                extra_info += f" [第{meta['page_number']}页]"
            if "row_index" in meta:
                extra_info += f" [第{meta['row_index']}行]"
            if "sheet_name" in meta:
                extra_info += f" [Sheet:{meta['sheet_name']}]"

            context_parts.append(
                f"【参考{i}】来源: {source_name} ({fmt}){extra_info}\n{text}"
            )

        return "\n\n".join(context_parts)

    def _call_llm(self, system_prompt: str, user_query: str) -> str:
        """调用 SiliconFlow DeepSeek API"""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_query},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            data = response.json()
            answer = data["choices"][0]["message"]["content"]
            return answer.strip()

        except requests.exceptions.RequestException as e:
            logger.error(f"[LLM] API 请求失败: {e}")
            return f"抱歉，调用模型时出错: {str(e)}"
        except (KeyError, IndexError) as e:
            logger.error(f"[LLM] 响应解析失败: {e}")
            return "抱歉，模型响应格式异常，请稍后重试。"

    def query(self, query: str, top_k: int = TOP_K, filter_dict: Optional[Dict] = None) -> Dict[str, Any]:
        """
        执行完整 RAG 查询

        Returns:
            {
                "query": str,          # 原始查询
                "answer": str,         # LLM 生成的答案
                "sources": List[dict], # 引用的参考资料列表
                "retrieved_count": int # 召回的 chunk 数量
            }
        """
        # 1. 检索
        chunks = self._retrieve(query, top_k=top_k, filter_dict=filter_dict)
        if not chunks:
            return {
                "query": query,
                "answer": "根据现有资料，未能找到相关信息。",
                "sources": [],
                "retrieved_count": 0,
            }

        # 2. 组装 context
        context = self._build_context(chunks)

        # 3. 组装 system prompt
        system_prompt = self.SYSTEM_PROMPT.format(context=context)

        # 4. 调用 LLM
        logger.info(f"[LLM] 调用 {self.model} 生成回答...")
        answer = self._call_llm(system_prompt, query)

        # 5. 格式化 sources
        sources = []
        for chunk in chunks:
            meta = chunk["metadata"]
            from pathlib import Path
            source_name = Path(meta.get("source", "")).name
            sources.append({
                "source": source_name,
                "format": meta.get("format", "unknown"),
                "score": round(chunk["score"], 4),
                "text_preview": chunk["text"][:200] + "..." if len(chunk["text"]) > 200 else chunk["text"],
            })

        return {
            "query": query,
            "answer": answer,
            "sources": sources,
            "retrieved_count": len(chunks),
        }

    def query_with_history(self, query: str, history: List[Dict[str, str]],
                           top_k: int = TOP_K) -> Dict[str, Any]:
        """
        带历史对话的 RAG 查询
        history: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
        """
        # 1. 检索（基于当前 query）
        chunks = self._retrieve(query, top_k=top_k)
        context = self._build_context(chunks) if chunks else "无相关参考资料"

        # 2. 组装 messages
        messages = [{"role": "system", "content": self.SYSTEM_PROMPT.format(context=context)}]
        messages.extend(history)
        messages.append({"role": "user", "content": query})

        # 3. 调用 LLM
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            data = response.json()
            answer = data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.error(f"[LLM] 请求失败: {e}")
            answer = f"抱歉，调用模型时出错: {str(e)}"

        sources = []
        for chunk in chunks:
            meta = chunk["metadata"]
            from pathlib import Path
            source_name = Path(meta.get("source", "")).name
            sources.append({
                "source": source_name,
                "format": meta.get("format", "unknown"),
                "score": round(chunk["score"], 4),
            })

        return {
            "query": query,
            "answer": answer,
            "sources": sources,
            "retrieved_count": len(chunks),
        }


# ==================== 便捷函数 ====================

def get_pipeline() -> RAGPipeline:
    """获取 RAGPipeline 实例"""
    return RAGPipeline()


if __name__ == "__main__":
    # 本地测试
    pipeline = get_pipeline()

    test_queries = [
        "路由器质保多久？",
        "铰链的单价是多少？",
        "新旧版本文档有什么冲突？",
    ]

    for q in test_queries:
        print(f"\n{'=' * 60}")
        print(f"问题: {q}")
        result = pipeline.query(q)
        print(f"\n回答: {result['answer']}")
        print(f"\n引用来源 ({result['retrieved_count']} 条):")
        for s in result["sources"]:
            print(f"  - [{s['format']}] {s['source']} (score={s['score']})")
