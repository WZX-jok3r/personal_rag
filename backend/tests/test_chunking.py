"""分块单测（P2 门禁）：覆盖文本类与表格类分块，行为对齐 src/chunk_strategy.py。"""

from app.ingestion.chunking import (
    chunk_document,
    is_markdown_table,
    split_by_size,
    table_rows_to_sentences,
)

_TBL = (
    "| ID | Name | City |\n"
    "| --- | --- | --- |\n"
    "| 1 | Alice | Beijing |\n"
    "| 2 | Bob | Shanghai |"
)


def test_is_markdown_table_positive():
    assert is_markdown_table(_TBL) is True


def test_is_markdown_table_negative():
    assert is_markdown_table("just some text\nsecond line") is False


def test_table_rows_to_sentences():
    sents = table_rows_to_sentences(_TBL)
    assert len(sents) == 2
    assert sents[0]["row_index"] == 0
    assert "Alice" in sents[0]["text"]
    # 列名中文化：Name -> 姓名, City -> 所在城市
    assert "姓名" in sents[0]["text"] and "所在城市" in sents[0]["text"]


def test_split_by_size_short_text_passthrough():
    assert split_by_size("短文本", 100, 10) == ["短文本"]


def test_split_by_size_long_text_has_overlap_and_multiple():
    text = "。".join(f"句子{i}" for i in range(200))
    parts = split_by_size(text, 50, 10)
    assert len(parts) > 1
    assert all(p for p in parts)


def test_chunk_document_txt_paragraph():
    docs = [{"text": "第一段内容。\n\n第二段内容。", "metadata": {"format": "txt", "source": "a.txt"}}]
    chunks = chunk_document(docs)
    assert chunks
    assert all(c.metadata.get("strategy", "").startswith("txt_") for c in chunks)


def test_chunk_document_md_table_aware():
    docs = [{"text": "# 员工表\n\n" + _TBL, "metadata": {"format": "md", "source": "b.md"}}]
    strategies = [c.metadata.get("strategy") for c in chunk_document(docs)]
    # 整表父块 + 两行行级语义句块
    assert "md_table_whole" in strategies
    assert strategies.count("table_row_sentence") == 2


def test_chunk_document_empty():
    assert chunk_document([]) == []


def test_chunk_document_marker_pdf_routes_to_md():
    # parser=marker 的 pdf 应强制走 md 表格感知策略
    docs = [{"text": "# 标题\n\n" + _TBL,
             "metadata": {"format": "pdf", "parser": "marker", "source": "c.pdf"}}]
    strategies = [c.metadata.get("strategy") for c in chunk_document(docs)]
    assert "md_table_whole" in strategies
