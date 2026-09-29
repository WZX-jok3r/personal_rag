"""
Source Formatter

职责：

将RAG检索结果转换为前端展示格式


输入:

chunks:

[
 {
   "text":"",
   "metadata":{},
   "score":0.9
 }
]


输出:

[
 {
   "source":"",
   "format":"",
   "score":"",
   "page":"",
   "preview":""
 }
]


"""

from pathlib import Path
from typing import List, Dict, Any


def format_sources(
        chunks: List[Dict[str, Any]],
        preview: bool = True
) -> List[Dict[str, Any]]:
    """
    格式化引用来源


    Args:

        chunks:
            检索结果


        preview:
            是否返回文本摘要


    Returns:

        sources列表

    """

    sources = []

    for chunk in chunks:

        metadata = chunk.get(
            "metadata",
            {}
        )

        source_path = metadata.get(
            "source",
            ""
        )

        filename = (
            Path(source_path).name
            if source_path
            else "未知文档"
        )

        item = {

            "source":
                filename,

            "format":
                metadata.get(
                    "format",
                    "unknown"
                ),

            "score":
                round(
                    chunk.get(
                        "score",
                        0
                    ),
                    4
                ),

        }

        # 页码

        if metadata.get(
                "page_number"
        ):
            item["page"] = (
                metadata["page_number"]
            )

        # 行号

        if metadata.get(
                "row_index"
        ):
            item["row"] = (
                metadata["row_index"]
            )

        # 文本预览

        if preview:
            text = chunk.get(
                "text",
                ""
            )

            item["preview"] = (

                text[:200] + "..."

                if len(text) > 200

                else text

            )

        sources.append(
            item
        )

    return sources
