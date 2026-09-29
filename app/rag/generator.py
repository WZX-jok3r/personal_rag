"""
LLM Generator

职责：

1. 管理LLM调用
2. 封装OpenAI兼容接口
3. 处理普通生成
4. 统一返回结果

不负责：

- 检索
- prompt构造
- context拼接
- source处理

"""

import logging
from typing import List, Dict, Any, Optional

from openai import OpenAI

from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL
)

logger = logging.getLogger(__name__)


class LLMGenerator:
    """
    LLM生成器

    当前:
        SiliconFlow

    后续:
        OpenAI
        Azure
        Claude
        本地模型

    只需要替换这里
    """

    def __init__(self):

        self.client = OpenAI(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL
        )

        self.model = LLM_MODEL

    def generate(
            self,
            messages: List[Dict[str, str]],
            temperature: float = 0.2
    ) -> str:

        """
        普通生成

        Args:
            messages:
                OpenAI格式消息


        Returns:
            answer
        """

        logger.info(
            "[LLM] start generation"
        )

        response = (
            self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature
            )
        )

        answer = (
            response
            .choices[0]
            .message
            .content
            .strip()
        )

        logger.info(
            "[LLM] generation finished"
        )

        return answer

    def generate_stream(
            self,
            messages: List[Dict[str, str]],
            temperature: float = 0.2
    ):

        """
        流式生成


        返回:

        token generator
        """

        logger.info(
            "[LLM] start streaming"
        )

        stream = (
            self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                stream=True
            )
        )

        for chunk in stream:

            delta = (
                chunk
                .choices[0]
                .delta
                .content
            )

            if delta:
                yield delta
