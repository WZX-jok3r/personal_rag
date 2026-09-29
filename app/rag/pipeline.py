"""
RAG Pipeline

职责：

负责串联整个RAG流程：

Query

↓

Retriever

↓

Context Builder

↓

Prompt

↓

Generator

↓

Source Formatter


不负责：

- 向量检索实现
- LLM实现
- Prompt内容
- 数据格式化
"""

import logging
from typing import Optional, Dict, Any

from .retriever import Retriever

from .context_builder import (
    build_context
)

from .generator import (
    LLMGenerator
)

from .prompt import (
    load_prompt
)

from .source_formatter import (
    format_sources
)

from app.core.config import settings

from .reranker import (
    RerankerFactory
)

logger = logging.getLogger(__name__)


class RAGPipeline:

    def __init__(
            self,
            prompt_name: str = "default"
    ):
        """
        初始化RAG流程


        prompt_name:

            使用哪个prompt模板

            default
            technical
            customer_service

        """

        self.retriever = Retriever()

        self.reranker = (
            RerankerFactory.create(
                settings.reranker.model_dump()
            )
        )

        self.generator = (
            LLMGenerator()
        )

        self.prompt_config = (
            load_prompt(
                prompt_name
            )
        )

    def query(
            self,
            query: str,
            top_k: int = 10,
            filter_dict: Optional[
                Dict[str, Any]
            ] = None
    ) -> Dict[str, Any]:
        """
        普通问答


        Returns:

        {
            answer:"",
            sources:[]
        }

        """

        logger.info(
            f"[RAG] query={query}"
        )

        # 1. Retrieval

        chunks, dropped = (
            self.retriever.search(
                query=query,
                top_k=top_k,
                filter_dict=filter_dict
            )
        )

        if not chunks:
            return {

                "answer":
                    "根据现有资料无法回答该问题。",

                "sources": [],

                "dropped": dropped

            }

        if settings.reranker.enabled:
            chunks = (
                self.reranker.rank(
                    query=query,
                    documents=chunks,
                    top_k=settings.reranker.top_k
                )
            )

        # 2. Context构造

        context = build_context(
            chunks,
            max_length=
            self.prompt_config.get(
                "max_context_length",
                12000
            )
        )

        # 3. Prompt

        system_prompt = (
            self.prompt_config[
                "system_prompt"
            ]
            .replace(
                "{context}",
                context
            )
        )

        messages = [

            {
                "role":
                    "system",

                "content":
                    system_prompt
            },

            {
                "role":
                    "user",

                "content":
                    query
            }

        ]

        # 4. LLM生成

        answer = (
            self.generator.generate(
                messages,
                temperature=
                self.prompt_config.get(
                    "temperature",
                    0.2
                )
            )
        )

        # 5. Source格式化

        sources = format_sources(
            chunks,
            preview=True
        )

        return {

            "query":
                query,

            "answer":
                answer,

            "sources":
                sources,

            "dropped":
                dropped

        }
