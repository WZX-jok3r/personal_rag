"""
RAG Streaming

职责：

处理RAG流式输出

流程:

query

↓

retrieve

↓

context

↓

prompt

↓

LLM stream

↓

yield token


不负责：

- API SSE响应封装
- FastAPI Response

这里只产生数据流
"""

import logging
from typing import Generator, Dict, Any

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

logger = logging.getLogger(__name__)


class RAGStream:

    def __init__(
            self,
            prompt_name="default"
    ):

        self.retriever = Retriever()

        self.generator = (
            LLMGenerator()
        )

        self.prompt_config = (
            load_prompt(
                prompt_name
            )
        )

    def stream(
            self,
            query: str,
            top_k: int = 10,
            filter_dict=None
    ) -> Generator[
        Dict[str, Any],
        None,
        None
    ]:

        """
        流式问答


        yield:

        {
          "type":"token",
          "content":"xxx"
        }


        最后：

        {
          "type":"sources",
          "data":[]
        }

        """

        logger.info(
            f"[Stream] query={query}"
        )

        # 1. retrieve

        chunks, dropped = (
            self.retriever.search(
                query=query,
                top_k=top_k,
                filter_dict=filter_dict
            )
        )

        if not chunks:
            yield {

                "type":
                    "token",

                "content":
                    "根据现有资料无法回答该问题。"

            }

            return

        # 2. context

        context = build_context(
            chunks,
            max_length=
            self.prompt_config.get(
                "max_context_length",
                12000
            )
        )

        # 3. prompt

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

        # 4. stream LLM

        for token in (
                self.generator.generate_stream(
                    messages,
                    temperature=
                    self.prompt_config.get(
                        "temperature",
                        0.2
                    )
                )
        ):
            yield {

                "type":
                    "token",

                "content":
                    token

            }

        # 5. sources

        yield {

            "type":
                "sources",

            "data":
                format_sources(
                    chunks,
                    preview=True
                )

        }
