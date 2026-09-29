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
from typing import List, Dict, Any, Optional, Iterator

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
import observability as obs

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class RAGPipeline:
    """RAG 问答管道"""

    # RAG System Prompt 模板
    SYSTEM_PROMPT = """你是一个专业的知识库问答助手。请严格根据以下提供的参考资料回答问题。
    **核心规则（按优先级）：**
    1. 优先使用最具体、最完整的信息片段
    2. 如果不同参考资料描述同一实体的不同方面 → 综合回答并指出信息来源
    3. 如果信息看似冲突但逻辑可调和 → 解释冲突可能的原因（如数据分类维度不同）
    4. 只有在完全无法确定时才说"根据现有资料无法回答"
    
    **信息融合策略：**
    - 多个表格片段 = 同一文档的不同视图，应合并理解
    - 细分地区数据包含在主表未列出的项目是正常的
    - 只要数据明确，就应基于该数据回答，然后提示局限性
    
    **表格数据特别处理原则：**
    1. 如果一个表格在多处出现（可能是分块造成的），将多个片段视为同一表格的不同部分
    2. 扩展表（包含更多行列）是对主表的补充，不是冲突
    3. "地区"的定义可以宽泛：洲级区域、国家、子区域都是有效地区
        
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

    def _retrieve(self, query: str, top_k: int = TOP_K, filter_dict: Optional[Dict] = None):
        """检索相关 chunk。返回 (chunks, dropped)，dropped 为被低相关过滤掉的条数"""
        logger.info(f"[Retrieve] 查询: {query}")
        results, stats = self.vector_store.search(query, top_k=top_k, filter_dict=filter_dict, with_stats=True)
        dropped = stats.get("dropped", 0)
        logger.info(f"[Retrieve] 召回 {len(results)} 条结果" + (f"（已过滤低相关 {dropped} 条）" if dropped else ""))
        return results, dropped

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

    def _format_sources(self, chunks: List[Dict[str, Any]], with_preview: bool = False) -> List[Dict[str, Any]]:
        """将检索 chunks 组装成 sources 列表（query / query_with_history / stream 共用）。
        with_preview=True 时附带正文预览。"""
        from pathlib import Path
        sources = []
        for chunk in chunks:
            meta = chunk["metadata"]
            source_name = Path(meta.get("source", "")).name
            item = {
                "source": source_name,
                "format": meta.get("format", "unknown"),
                "score": round(chunk["score"], 4),
            }
            if with_preview:
                text = chunk["text"]
                item["text_preview"] = text[:200] + "..." if len(text) > 200 else text
            sources.append(item)
        return sources

    def _call_llm(self, system_prompt: str, user_query: str) -> str:
        """调用 SiliconFlow DeepSeek API（保持旧签名：仅返回答案文本）。
        实际请求与 usage 捕获都委派给 _do_llm_request，作为唯一埋点入口。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ]
        answer, _usage = self._do_llm_request(messages)
        return answer

    def _do_llm_request(self, messages: List[Dict[str, str]]) -> (str, Dict[str, Any]):
        """
        统一的 LLM 请求入口（query 与 query_with_history 共用）。
        - 在一个 Langfuse generation 下发起请求，记录 input/output 与 token usage；
          可观测未启用时 generation 为 no-op，行为与之前完全一致。
        - 返回 (答案文本, usage 字典)；异常时返回友好提示与空 usage。
        """
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

        with obs.generation("llm", model=self.model, input=messages) as gen:
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=120)
                response.raise_for_status()
                data = response.json()
                answer = data["choices"][0]["message"]["content"].strip()
                usage = obs.extract_usage(data)
                gen.update(output=answer, usage=usage, metadata={"model": self.model})
                return answer, usage

            except requests.exceptions.RequestException as e:
                logger.error(f"[LLM] API 请求失败: {e}")
                gen.update(output=f"抱歉，调用模型时出错: {str(e)}", level="ERROR", status_message=str(e))
                return f"抱歉，调用模型时出错: {str(e)}", {}
            except (KeyError, IndexError) as e:
                logger.error(f"[LLM] 响应解析失败: {e}")
                gen.update(output="抱歉，模型响应格式异常。", level="ERROR", status_message=str(e))
                return "抱歉，模型响应格式异常，请稍后重试。", {}

    def _stream_llm(self, messages: List[Dict[str, str]]) -> Iterator[str]:
        """流式调用 SiliconFlow chat/completions（stream=True），逐段 yield 文本增量。
        - 仅处理 `data:` 帧；`[DONE]` 终止；无 choices 的纯 usage 帧只记录不产出。
        - 全程包在 obs.generation 下，结束时以累计全文与 usage 更新；请求异常向上抛。"""
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
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        with obs.generation("llm", model=self.model, input=messages) as gen:
            parts: List[str] = []
            usage: Dict[str, Any] = {}
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=120, stream=True)
                resp.raise_for_status()
                # SSE 帧为 UTF-8；响应头无 charset 时 iter_lines(decode_unicode=True) 会误用 latin-1，
                # 故按字节行读取后显式以 UTF-8 解码（\n=0x0A 不会出现在 UTF-8 多字节序列中，逐行解码安全）
                for raw in resp.iter_lines():
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace")
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = obs.extract_usage(chunk)
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        parts.append(delta)
                        yield delta
                gen.update(output="".join(parts).strip(), usage=usage,
                           metadata={"model": self.model, "stream": True})
            except requests.exceptions.RequestException as e:
                logger.error(f"[LLM stream] 请求失败: {e}")
                gen.update(output="".join(parts), level="ERROR", status_message=str(e))
                raise

    def stream_chat(self, query: str, history: List[Dict[str, str]],
                    top_k: int = TOP_K, filter_dict: Optional[Dict] = None) -> Iterator[Dict[str, Any]]:
        """流式多轮问答：依次 yield meta -> delta(多个) -> done/error 事件字典。
        检索、低相关过滤与来源组装与非流式 query_with_history 一致，不改动。"""
        with obs.trace("rag_chat_stream", input={"query": query, "top_k": top_k, "history_len": len(history)},
                        metadata={"filter": filter_dict}) as tr:
            with obs.span("retrieve", input=query) as sp:
                chunks, dropped = self._retrieve(query, top_k=top_k, filter_dict=filter_dict)
                sp.update(output={"retrieved_count": len(chunks), "hidden_count": dropped})

            sources = self._format_sources(chunks, with_preview=False)
            yield {"type": "meta", "sources": sources,
                   "retrieved_count": len(chunks), "hidden_count": dropped}

            if not chunks:
                refuse = "根据现有资料，未能找到相关信息。"
                yield {"type": "delta", "text": refuse}
                yield {"type": "done", "answer": refuse}
                tr.update(output=refuse, metadata={"retrieved_count": 0})
                return

            context = self._build_context(chunks)
            messages = [{"role": "system", "content": self.SYSTEM_PROMPT.format(context=context)}]
            messages.extend(history)
            messages.append({"role": "user", "content": query})

            parts: List[str] = []
            try:
                for delta in self._stream_llm(messages):
                    parts.append(delta)
                    yield {"type": "delta", "text": delta}
                answer = "".join(parts).strip()
                tr.update(output=answer, metadata={"retrieved_count": len(chunks),
                                                   "sources": [s["source"] for s in sources]})
                yield {"type": "done", "answer": answer}
            except requests.exceptions.RequestException as e:
                yield {"type": "error", "message": f"抱歉，调用模型时出错: {str(e)}"}

    def query(self, query: str, top_k: int = TOP_K, filter_dict: Optional[Dict] = None,
              generate: bool = True) -> Dict[str, Any]:
        """
        执行完整 RAG 查询

        Args:
            generate: True=检索+LLM 生成；False=仅检索（评测快速模式，跳过 LLM 调用）

        Returns:
            {
                "query": str,          # 原始查询
                "answer": str,         # LLM 生成的答案
                "sources": List[dict], # 引用的参考资料列表
                "retrieved_count": int # 召回的 chunk 数量
            }
        """
        # 1. 检索
        with obs.trace("rag_query", input={"query": query, "top_k": top_k, "generate": generate},
                        metadata={"filter": filter_dict}) as tr:
            with obs.span("retrieve", input=query) as sp:
                chunks, dropped = self._retrieve(query, top_k=top_k, filter_dict=filter_dict)
                sp.update(output={"retrieved_count": len(chunks), "hidden_count": dropped})
            if not chunks:
                result = {
                    "query": query,
                    "answer": "根据现有资料，未能找到相关信息。",
                    "sources": [],
                    "retrieved_count": 0,
                    "hidden_count": dropped,
                }
                tr.update(output=result["answer"], metadata={"retrieved_count": 0})
                return result

            # 2~4. 组装 context + 调用 LLM（retrieval-only 模式跳过生成环节）
            if generate:
                context = self._build_context(chunks)
                system_prompt = self.SYSTEM_PROMPT.format(context=context)
                logger.info(f"[LLM] 调用 {self.model} 生成回答...")
                answer = self._call_llm(system_prompt, query)
            else:
                answer = "(retrieval-only：已跳过 LLM 生成)"

            # 5. 格式化 sources
            sources = self._format_sources(chunks, with_preview=True)

            result = {
                "query": query,
                "answer": answer,
                "sources": sources,
                "retrieved_count": len(chunks),
                "hidden_count": dropped,
            }
            tr.update(output=answer, metadata={"retrieved_count": len(chunks),
                                                "sources": [s["source"] for s in sources]})
            return result

    def query_with_history(self, query: str, history: List[Dict[str, str]],
                           top_k: int = TOP_K, filter_dict: Optional[Dict] = None) -> Dict[str, Any]:
        """
        带历史对话的 RAG 查询
        history: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
        filter_dict: 服务端强制的元数据过滤（如多租户 tenant_id 隔离）
        """
        # 1. 检索（基于当前 query，叠加服务端强制过滤）
        with obs.trace("rag_chat", input={"query": query, "top_k": top_k, "history_len": len(history)},
                        metadata={"filter": filter_dict}) as tr:
            with obs.span("retrieve", input=query) as sp:
                chunks, dropped = self._retrieve(query, top_k=top_k, filter_dict=filter_dict)
                sp.update(output={"retrieved_count": len(chunks), "hidden_count": dropped})
            context = self._build_context(chunks) if chunks else "无相关参考资料"

            # 2. 组装 messages
            messages = [{"role": "system", "content": self.SYSTEM_PROMPT.format(context=context)}]
            messages.extend(history)
            messages.append({"role": "user", "content": query})

            # 3. 调用 LLM（统一入口，内部带 generation 埋点与 usage 捕获）
            answer, _usage = self._do_llm_request(messages)

            sources = self._format_sources(chunks, with_preview=False)

            result = {
                "query": query,
                "answer": answer,
                "sources": sources,
                "retrieved_count": len(chunks),
                "hidden_count": dropped,
            }
            tr.update(output=answer, metadata={"retrieved_count": len(chunks),
                                                "sources": [s["source"] for s in sources]})
            return result


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
