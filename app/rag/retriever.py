"""
Retriever

职责：
1. 接收用户query
2. 调用vector store
3. 返回召回chunks
4. 返回过滤统计信息

注意：
这里不负责：
- embedding
- qdrant实现
- bm25实现
- rerank

这些属于底层组件。
"""

import logging
from typing import Optional, Dict, Any, Tuple, List

from vector_store import (
    get_vector_store,
    VectorStore
)

from config import TOP_K

logger = logging.getLogger(__name__)


class Retriever:
    """
    RAG Retriever

    负责从知识库中召回相关文档片段
    """

    def __init__(self):
        self.vector_store: VectorStore = (
            get_vector_store()
        )

    def search(
            self,
            query: str,
            top_k: int = TOP_K,
            filter_dict: Optional[Dict[str, Any]] = None
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        检索相关chunks

        Args:
            query:用户问题
            top_k:返回数量
            filter_dict:元数据过滤
            未来用于:tenant_id隔离

        Returns:
            chunks:检索结果
            dropped:被低相关过滤数量
        """

        logger.info(
            f"[Retriever] query={query}"
        )

        results, stats = (
            self.vector_store.search(
                query,
                top_k=top_k,
                filter_dict=filter_dict,
                with_stats=True
            )
        )

        dropped = stats.get(
            "dropped",
            0
        )

        logger.info(
            "[Retriever] "
            f"retrieved={len(results)}, "
            f"dropped={dropped}"
        )

        return (
            results,
            dropped
        )
