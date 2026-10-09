"""RAG 链路编排单测（P2 门禁）：注入 FakeRetriever / FakeLLM，验证三条对外链路的
结果键契约、无召回拒答、检索-only 跳过生成、流式事件序列。不触网、不查库。
"""

from app.rag.pipeline import RAGPipeline

_CHUNKS = [
    {
        "text": "员工表：编号1，姓名Alice，所在城市Beijing。",
        "metadata": {"source": "data/hr.md", "format": "md"},
        "score": 0.88,
    }
]


class FakeRetriever:
    def __init__(self, chunks, dropped=0):
        self._chunks = chunks
        self._dropped = dropped
        self.calls = []

    def search(self, query, top_k=None, filter_dict=None):
        self.calls.append((query, top_k, filter_dict))
        return self._chunks, self._dropped


class FakeLLM:
    model = "fake-model"

    def complete(self, messages, temperature=None, max_tokens=None):
        return "这是答案", {"input": 1, "output": 2, "total": 3}

    def stream(self, messages, temperature=None, max_tokens=None):
        yield "你"
        yield "好"


def test_query_result_contract():
    p = RAGPipeline(retriever=FakeRetriever(_CHUNKS, dropped=2), llm=FakeLLM())
    res = p.query("问题", top_k=5)

    assert set(res.keys()) == {
        "query", "answer", "sources", "retrieved_count", "hidden_count",
    }
    assert res["answer"] == "这是答案"
    assert res["retrieved_count"] == 1
    assert res["hidden_count"] == 2
    # query 走 with_preview=True
    assert res["sources"][0]["source"] == "hr.md"
    assert "text_preview" in res["sources"][0]


def test_query_retrieval_only_skips_llm():
    p = RAGPipeline(retriever=FakeRetriever(_CHUNKS), llm=FakeLLM())
    res = p.query("问题", generate=False)
    assert res["answer"] == "(retrieval-only：已跳过 LLM 生成)"


def test_query_no_context_refuses():
    p = RAGPipeline(retriever=FakeRetriever([], dropped=0), llm=FakeLLM())
    res = p.query("问题")
    assert res["answer"] == "根据现有资料，未能找到相关信息。"
    assert res["sources"] == []
    assert res["retrieved_count"] == 0


def test_query_with_history_uses_fallback_context():
    # 无召回时不拒答，走 "无相关参考资料" 兜底，仍调用 LLM
    p = RAGPipeline(retriever=FakeRetriever([]), llm=FakeLLM())
    res = p.query_with_history("问题", history=[{"role": "user", "content": "早"}])
    assert res["answer"] == "这是答案"
    assert res["sources"] == []


def test_stream_chat_event_sequence():
    p = RAGPipeline(retriever=FakeRetriever(_CHUNKS), llm=FakeLLM())
    events = list(p.stream_chat("问题", history=[]))
    types = [e["type"] for e in events]

    assert types[0] == "meta"
    assert types[-1] == "done"
    assert "delta" in types
    assert events[0]["sources"][0]["source"] == "hr.md"
    assert events[-1]["answer"] == "你好"


def test_stream_chat_no_context_short_circuits():
    p = RAGPipeline(retriever=FakeRetriever([]), llm=FakeLLM())
    events = list(p.stream_chat("问题", history=[]))
    assert events[0]["type"] == "meta"
    assert events[-1]["type"] == "done"
    assert events[-1]["answer"] == "根据现有资料，未能找到相关信息。"
