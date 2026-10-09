"""_diag_recall.py - 评测检索失败题的关卡定位（诊断用，不改动业务代码）

思路：对 eval 结果里 passed=False 的题，沿检索漏斗逐关探测：
  Gate0 该 source 在 Qdrant 是否有向量点（入库层）
  Gate1 hybrid(dense+BM25→RRF) 前 20 候选里它的排名（召回层）
  Gate2 rerank 精排后是否进前 5、绝对分多少（排序层）
  Gate3 分数是否低于 score_abs_min 会被低相关过滤（过滤层）

用法（backend 目录下）：
    uv run python _diag_recall.py [eval_json_path]
报告写到项目根 output/diag_recall.txt（UTF-8，避开控制台编码问题）。
"""
import json
import sys
from pathlib import Path

from qdrant_client.models import Filter, FieldCondition, MatchValue

from app.core.config import settings
from app.vector.qdrant import get_vector_store


def norm(s: str) -> str:
    """与评测器一致：各种破折号归一化为标准短横线"""
    if not s:
        return s
    return s.replace("\u2011", "-").replace("–", "-").replace("—", "-")


def main():
    eval_json = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        settings.base_dir / "output" / "eval_retrieval_docker.json"
    data = json.loads(eval_json.read_text(encoding="utf-8"))
    fails = [r for r in data["results"] if r.get("passed") is False]

    vs = get_vector_store()
    limit = max(settings.top_k, settings.rerank_candidates)
    lines = [f"评测结果: {eval_json.name} | 失败题 {len(fails)} 道",
             f"参数: mode={settings.retrieval_mode} candidates={limit} top_k={settings.top_k} "
             f"abs_min={settings.score_abs_min} rerank={'on' if vs.rerank_client else 'off'}",
             "=" * 70]

    for r in fails:
        q, exp = r["question"], norm(r["expected_source"])

        # Gate0: 库内点数
        cnt = vs.client.count(
            collection_name=vs.collection_name,
            count_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=exp))]),
            exact=True,
        ).count

        # Gate1: hybrid 候选
        vec = vs.embedding_client.embed_single(q)
        res = vs._hybrid_query(q, vec, limit, None)
        srcs = [norm(p.payload.get("source", "")) for p in res.points]
        cand_rank = srcs.index(exp) + 1 if exp in srcs else -1

        # Gate2: rerank 后前5 与 期望文件绝对分
        exp_score, top5 = None, []
        rr = vs._rerank_points(q, res.points, settings.top_k) if vs.rerank_client else None
        if rr is not None:
            pairs = [(norm(p.payload.get("source", "")), round(s, 4)) for p, s in rr]
            top5 = [s for s, _ in pairs]
            exp_score = next((sc for s, sc in pairs if s == exp), None)
        else:
            top5 = srcs[: settings.top_k]

        in_top5 = exp in top5

        # 定位结论
        if cnt == 0:
            verdict = "Gate0 未入库：Qdrant 没有该文件的向量"
        elif cand_rank == -1:
            verdict = "Gate1 召回 miss：RRF 前 20 候选都没进（分块/embedding/BM25 词面问题）"
        elif not in_top5:
            verdict = f"Gate2 精排出局：候选第 {cand_rank}，但 rerank 没送进前 5"
        elif exp_score is not None and exp_score < settings.score_abs_min:
            verdict = f"Gate3 被低相关过滤：分 {exp_score} < abs_min {settings.score_abs_min}"
        else:
            verdict = "本题实际可命中（检查评测比对口径）"

        lines.append(f"\nQ: {q[:60]}")
        lines.append(f"期望: {exp}")
        lines.append(f"  Gate0 库内点数={cnt} | Gate1 候选排名={cand_rank} | "
                     f"Gate2 进前5={in_top5} (rerank分={exp_score}) ")
        lines.append(f"  => {verdict}")
        lines.append(f"  实际前5: {top5}")

    out = settings.base_dir / "output" / "diag_recall.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"DONE -> {out}")


if __name__ == "__main__":
    main()
