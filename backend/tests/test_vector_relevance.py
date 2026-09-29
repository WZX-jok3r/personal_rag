"""vector 层纯函数单测（P2 门禁）：低相关过滤的量纲安全分支 + BM25 英数串保护分词。

_filter_low_relevance / tokenize_for_bm25 只读 settings 阈值与本地逻辑，无外部依赖，
通过 monkeypatch settings.score_* 覆盖各分支。
"""

from app.vector import qdrant as q


def _results(scores):
    return [{"score": s} for s in scores]


def test_non_rerank_score_kind_passthrough(monkeypatch):
    # rrf/vector 分数量纲不可比，绝不套用绝对阈值误杀
    monkeypatch.setattr(q.settings, "score_abs_min", 0.05)
    results = _results([0.9, 0.001])
    kept, dropped = q._filter_low_relevance(results, score_kind="rrf")
    assert kept == results
    assert dropped == 0


def test_filter_disabled_when_abs_min_nonpositive(monkeypatch):
    monkeypatch.setattr(q.settings, "score_abs_min", 0)
    results = _results([0.9, 0.1])
    kept, dropped = q._filter_low_relevance(results, "rerank")
    assert kept == results
    assert dropped == 0


def test_all_below_abs_min_drop_all(monkeypatch):
    monkeypatch.setattr(q.settings, "score_abs_min", 0.3)
    monkeypatch.setattr(q.settings, "score_drop_all_below", True)
    results = _results([0.2, 0.1])
    kept, dropped = q._filter_low_relevance(results, "rerank")
    assert kept == []
    assert dropped == 2


def test_all_below_abs_min_keep_floor(monkeypatch):
    monkeypatch.setattr(q.settings, "score_abs_min", 0.3)
    monkeypatch.setattr(q.settings, "score_drop_all_below", False)
    monkeypatch.setattr(q.settings, "score_min_keep", 1)
    results = _results([0.2, 0.15, 0.1])
    kept, dropped = q._filter_low_relevance(results, "rerank")
    assert len(kept) == 1
    assert kept[0]["score"] == 0.2
    assert dropped == 2


def test_strong_anchor_enables_relative_fault(monkeypatch):
    monkeypatch.setattr(q.settings, "score_abs_min", 0.05)
    monkeypatch.setattr(q.settings, "score_confident", 0.5)
    monkeypatch.setattr(q.settings, "score_rel_ratio", 0.15)
    # max=0.9>=confident → threshold=max(0.05, 0.9*0.15=0.135)=0.135
    results = _results([0.9, 0.2, 0.1, 0.05])
    kept, dropped = q._filter_low_relevance(results, "rerank")
    assert [r["score"] for r in kept] == [0.9, 0.2]
    assert dropped == 2


def test_mid_scores_only_abs_floor_no_fault(monkeypatch):
    monkeypatch.setattr(q.settings, "score_abs_min", 0.05)
    monkeypatch.setattr(q.settings, "score_confident", 0.5)
    monkeypatch.setattr(q.settings, "score_rel_ratio", 0.15)
    # max=0.4<confident → 仅用绝对下限 0.05，不对中等分簇施断层
    results = _results([0.4, 0.3, 0.04])
    kept, dropped = q._filter_low_relevance(results, "rerank")
    assert [r["score"] for r in kept] == [0.4, 0.3]
    assert dropped == 1


def test_tokenize_protects_ascii_runs():
    # 型号/SKU 这类英数串必须整体保留，不被 jieba 拆碎
    out = q.tokenize_for_bm25("适配器 PS-12V-1.5A 输出电压")
    assert "PS-12V-1.5A" in out.split()
