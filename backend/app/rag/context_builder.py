"""
context_builder.py - 把检索结果拼装成 LLM 上下文文本

忠实迁移自 src/rag_pipeline.py 的 _build_context：保留【参考N】编号、来源文件名、
格式、页码/行号/Sheet 等附加信息，格式与换行完全一致，避免上下文漂移影响回答质量。
"""

from pathlib import Path
from typing import Any, Dict, List


def build_context(chunks: List[Dict[str, Any]]) -> str:
    """将检索结果组装成 context 文本。"""
    context_parts = []
    for i, chunk in enumerate(chunks, 1):
        text = chunk["text"]
        meta = chunk["metadata"]
        source = meta.get("source", "未知来源")
        fmt = meta.get("format", "未知格式")

        # 提取文件名
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
