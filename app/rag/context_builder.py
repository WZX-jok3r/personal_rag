"""
Context Builder

职责：

将Retriever返回的chunks
转换成LLM可理解的context文本


输入：

[
 {
    "text":"xxx",
    "metadata":{}
 }
]


输出：

字符串context


不负责：

- 检索
- 排序
- LLM调用

"""

import logging
from pathlib import Path
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


def build_context(
        chunks: List[Dict[str, Any]],
        max_length: int = 12000
) -> str:
    """
    构造LLM上下文


    Args:

        chunks:
            检索结果


        max_length:
            最大字符长度


    Returns:

        context字符串

    """

    if not chunks:
        return ""

    contexts = []

    current_length = 0

    for index, chunk in enumerate(chunks, start=1):

        text = chunk.get(
            "text",
            ""
        )

        metadata = chunk.get(
            "metadata",
            {}
        )

        source = metadata.get(
            "source",
            "unknown"
        )

        filename = Path(
            source
        ).name

        location = []

        if metadata.get("page_number"):
            location.append(
                f"第{metadata['page_number']}页"
            )

        if metadata.get("row_index"):
            location.append(
                f"第{metadata['row_index']}行"
            )

        location_text = (
            " / ".join(location)
            if location
            else "未知位置"
        )

        block = f"""
======== 参考资料 {index} ========

来源:
{filename}

位置:
{location_text}


内容:
{text}

"""

        # 简单长度控制
        if (
                current_length +
                len(block)
                >
                max_length
        ):
            logger.info(
                "[Context] "
                "reach max length"
            )

            break

        contexts.append(
            block
        )

        current_length += len(block)

    return "\n".join(contexts)
