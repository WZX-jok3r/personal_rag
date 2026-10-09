"""检索读路径单测（P2 门禁）：Retriever 是 VectorStore.search 的薄封装。

用 FakeVectorStore 注入，断言 (chunks, dropped) 收敛与参数透传，不触碰真实 Qdrant。
"""

from app.core.config import settings
from app.rag.retriever import Retriever


class FakeVectorStore:
    def __init__(self, results, stats):
        self._results = results
        self._stats = stats
        self.calls = []

    def search(self, query, top_k=None, filter_dict=None, with_stats=False):
        self.calls.append({
            "query": query, "top_k": top_k,
            "filter_dict": filter_dict, "with_stats": with_stats,
        })
        return self._results, self._stats


def test_search_returns_chunks_and_dropped():
    results = [{"text": "a", "metadata": {"source": "x.md"}, "score": 0.9}]
    fake = FakeVectorStore(results, {"dropped": 3})
    r = Retriever(vector_store=fake)

    chunks, dropped = r.search("q", top_k=7, filter_dict={"tenant_id": "t1"})

    assert chunks == results
    assert dropped == 3
    call = fake.calls[0]
    assert call["with_stats"] is True
    assert call["top_k"] == 7
    assert call["filter_dict"] == {"tenant_id": "t1"}


def test_search_dropped_defaults_to_zero():
    fake = FakeVectorStore([], {})  # stats 无 dropped 键
    r = Retriever(vector_store=fake)
    chunks, dropped = r.search("q")
    assert chunks == []
    assert dropped == 0


def test_search_default_top_k_from_settings():
    fake = FakeVectorStore([], {"dropped": 0})
    r = Retriever(vector_store=fake)
    r.search("q")
    assert fake.calls[0]["top_k"] == settings.top_k
