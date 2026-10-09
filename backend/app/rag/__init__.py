"""RAG 领域：检索 -> 上下文 -> 生成 -> 来源，及流式问答编排。"""

from app.rag.pipeline import RAGPipeline, get_pipeline

__all__ = ["RAGPipeline", "get_pipeline"]
