"""
Query Rewriter


职责:

根据历史对话，
将用户当前问题改写成完整查询。


输入:

history + query


输出:

rewritten_query



不负责:

- 检索
- 生成答案
- session管理


"""

import logging

from typing import List, Dict

from app.core.config import settings

from .generator import (
    LLMGenerator
)

logger = logging.getLogger(__name__)


class QueryRewriter:

    def __init__(
            self
    ):

        self.generator = (
            LLMGenerator()
        )

    def rewrite(
            self,
            query: str,
            history: List[Dict] | None = None
    ) -> str:

        """
        将问题改写为独立查询


        """

        # 没有历史
        # 不需要改写

        if not history:
            return query

        prompt = f"""
你是一个搜索查询优化助手。

你的任务：

根据历史对话，
将用户当前问题改写成一个完整的问题。

要求：

1. 保留用户真实意图
2. 补充必要上下文
3. 不回答问题
4. 只输出改写后的查询


历史对话：

{self._format_history(history)}


当前问题：

{query}


改写后的查询：
"""

        messages = [

            {
                "role":
                    "user",

                "content":
                    prompt
            }

        ]

        try:

            result = (
                self.generator.generate(
                    messages,
                    temperature=0
                )
            )

            rewritten = (
                result.strip()
            )

            if rewritten:
                logger.info(
                    "[QueryRewrite] "
                    f"{query} -> {rewritten}"
                )

                return rewritten



        except Exception as e:

            logger.warning(
                f"Query rewrite failed: {e}"
            )

        return query

    def _format_history(
            self,
            history: List[Dict]
    ) -> str:

        """
        格式化历史消息
        """

        texts = []

        for item in history[-6:]:
            role = (
                item.get(
                    "role",
                    ""
                )
            )

            content = (
                item.get(
                    "content",
                    ""
                )
            )

            texts.append(
                f"{role}: {content}"
            )

        return "\n".join(texts)
