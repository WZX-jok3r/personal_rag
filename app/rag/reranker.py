"""
Reranker Module


职责:

对Retriever召回结果重新排序


输入:

query

documents


输出:

排序后的documents



不负责:

- embedding
- qdrant查询
- LLM


"""

import logging

from typing import List, Dict, Any

logger = logging.getLogger(__name__)


class BaseReranker:

    def rank(
            self,
            query: str,
            documents: List[Dict[str, Any]],
            top_k: int = 5
    ):
        raise NotImplementedError


class NoopReranker(BaseReranker):
    """
    默认空实现


    当没有配置reranker时:

保持原始召回顺序。


    """

    def rank(
            self,
            query,
            documents,
            top_k=5
    ):
        return documents[:top_k]


class SiliconFlowReranker(
    BaseReranker
):
    """
    SiliconFlow Reranker


    预留接口


    后续接:

    BGE-reranker

    """

    def __init__(
            self,
            api_key=None,
            model=None
    ):
        self.api_key = api_key

        self.model = model

    def rank(
            self,
            query,
            documents,
            top_k=5
    ):
        """
        TODO:

        调用rerank API


        返回:

        [
          {
            "content":"",
            "score":0.9
          }
        ]

        """

        logger.warning(
            "Reranker not implemented, fallback"
        )

        return documents[:top_k]


class RerankerFactory:

    @staticmethod
    def create(
            config=None
    ):

        """
        根据配置创建reranker
        """

        if not config:
            return NoopReranker()

        provider = (
            config.get(
                "provider",
                "none"
            )
        )

        if provider == "siliconflow":
            return SiliconFlowReranker(
                api_key=
                config.get(
                    "api_key"
                ),

                model=
                config.get(
                    "model"
                )
            )

        return NoopReranker()
