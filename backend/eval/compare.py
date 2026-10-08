"""
compare.py - 评测结果回归比对（每个小步改造后的验证闸口）

用途：把「当前评测结果」与「冻结的基线」逐题比对，而不是只看聚合指标。
为什么必须逐题：聚合指标会掩盖对冲——比如 A 题修好了、B 题坏了，
召回率仍是 99.01%，但行为已经变了。逐题比对才能发现这种"等量置换"。

用法:
    # 生成（或刷新）基线快照
    python -m eval.compare --snapshot            # 跑评测并冻结为基线
    python -m eval.compare --snapshot --from output/eval_result.json   # 从已有结果冻结

    # 回归比对（改造后执行）
    python -m eval.compare --baseline baseline_v1 --current output/eval_result.json

退出码:
    0 = 无回归
    1 = 检测到回归（可用于 CI 门禁）

比对口径:
    - 逐题 recall_at_k / source_rank / passed 必须不劣化
    - 聚合指标（pass_rate / recall_at_k_rate / mrr）不得下降（允许浮点误差 1e-9）
    - "召回仍在但排名下降" 也判为回归（rank 变差会影响 LLM 拿到的上下文质量）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# 与控制台编码对齐（Windows 重定向时默认 GBK 会崩）
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 基线快照目录：放在包内（而不是 output/）以便纳入 git 版本管理
BASELINE_DIR = Path(__file__).resolve().parent / "baseline"

_EPS = 1e-9


# ==================== 数据加载 ====================

def _load(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _index_by_question(results: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """按 question 建索引。重复题面保留首次出现（评测集不应有重复，防御性处理）。"""
    idx: Dict[str, Dict[str, Any]] = {}
    for r in results:
        q = r.get("question", "")
        if q and q not in idx:
            idx[q] = r
    return idx


def snapshot_baseline(src_json: Path, name: str) -> Path:
    """把一次评测结果冻结为基线快照（只保留比对必需字段，控制文件体积）。"""
    data = _load(src_json)
    report = data.get("report", {})
    results = data.get("results", [])
    if not results:
        raise ValueError(f"评测结果缺少 results 数组: {src_json}")

    slim = [
        {
            "question": r.get("question", ""),
            "expected_source": r.get("expected_source", ""),
            "recall_at_k": r.get("recall_at_k"),
            "source_rank": r.get("source_rank", -1),
            "passed": r.get("passed"),
        }
        for r in results
    ]

    baseline = {
        "name": name,
        "summary": report.get("summary", {}),
        "results": slim,
    }

    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    out = BASELINE_DIR / f"{name}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)
    return out


# ==================== 比对核心 ====================

def compare(baseline: Dict[str, Any], current: Dict[str, Any]) -> Tuple[bool, List[str], List[str]]:
    """
    返回 (是否有回归, 回归明细列表, 改善明细列表)。

    纯函数，便于单测：不读文件、不打印。
    """
    regressions: List[str] = []
    improvements: List[str] = []

    b_sum = baseline.get("summary", {})
    c_sum = current.get("report", {}).get("summary", {})

    # ---- 1) 聚合指标不得下降 ----
    for key, label in (
        ("pass_rate", "通过率"),
        ("recall_at_k_rate", "Recall@K"),
        ("mrr", "MRR"),
    ):
        bv = b_sum.get(key)
        cv = c_sum.get(key)
        if bv is None or cv is None:
            regressions.append(f"[聚合] {label} 缺失（基线={bv}, 当前={cv}）")
            continue
        if cv < bv - _EPS:
            regressions.append(f"[聚合] {label} 下降: {bv:.4f} → {cv:.4f}")
        elif cv > bv + _EPS:
            improvements.append(f"[聚合] {label} 提升: {bv:.4f} → {cv:.4f}")

    # ---- 2) 逐题比对 ----
    b_idx = _index_by_question(baseline.get("results", []))
    c_idx = _index_by_question(current.get("results", []))

    missing = sorted(set(b_idx) - set(c_idx))
    added = sorted(set(c_idx) - set(b_idx))
    for q in missing:
        regressions.append(f"[缺题] 基线有但当前缺失: {q[:60]}")
    for q in added:
        improvements.append(f"[新题] 当前新增: {q[:60]}")

    for q in sorted(set(b_idx) & set(c_idx)):
        b, c = b_idx[q], c_idx[q]
        short = q[:50]

        b_recall, c_recall = b.get("recall_at_k"), c.get("recall_at_k")
        # 召回丢失 = 最严重回归
        if b_recall and not c_recall:
            regressions.append(
                f"[丢召回] {short} | 期望源 {b.get('expected_source')} 不再被召回（原 rank={b.get('source_rank')}）"
            )
            continue

        b_rank, c_rank = b.get("source_rank", -1), c.get("source_rank", -1)
        # 排名下降（含从命中变为未命中）
        if b_recall and c_recall and _rank_bad(b_rank, c_rank):
            regressions.append(f"[排名下降] {short} | rank {b_rank} → {c_rank}")
        # 排名上升
        elif b_recall and c_recall and _rank_bad(c_rank, b_rank):
            improvements.append(f"[排名上升] {short} | rank {b_rank} → {c_rank}")

        # passed 由 recall 与关键词共同决定；retrieval-only 下关键词恒为 N/A
        if b.get("passed") and not c.get("passed"):
            regressions.append(f"[通过→失败] {short}")

    return (len(regressions) > 0), regressions, improvements


def _rank_bad(old_rank: int, new_rank: int) -> bool:
    """判断排名是否变差。-1 表示未召回（视为最差）。"""
    if old_rank == new_rank:
        return False
    if new_rank == -1:
        return True
    if old_rank == -1:
        return False
    return new_rank > old_rank


# ==================== CLI ====================

def main() -> int:
    parser = argparse.ArgumentParser(description="评测回归比对 / 基线冻结")
    parser.add_argument("--snapshot", action="store_true", help="把评测结果冻结为基线快照")
    parser.add_argument("--from", dest="from_json", type=str, default=None,
                        help="快照来源 JSON（默认 output/eval_result.json）")
    parser.add_argument("--name", type=str, default="baseline_v1", help="基线名（决定文件名）")
    parser.add_argument("--baseline", type=str, default="baseline_v1", help="要比对的基线名")
    parser.add_argument("--current", type=str, default=None,
                        help="当前评测结果 JSON（默认 output/eval_result.json）")
    args = parser.parse_args()

    # 输出目录锚定项目根（与 eval.runner 口径一致）
    from app.core.config import settings
    output_dir = settings.base_dir / "output"
    default_current = output_dir / "eval_result.json"

    if args.snapshot:
        src = Path(args.from_json) if args.from_json else default_current
        if not src.exists():
            print(f"[基线] 源文件不存在: {src}", file=sys.stderr)
            return 1
        out = snapshot_baseline(src, args.name)
        data = _load(out)
        s = data["summary"]
        print(f"[基线] 已冻结: {out}")
        print(f"       {len(data['results'])} 题 | 通过率 {s.get('pass_rate')} | "
              f"Recall@K {s.get('recall_at_k_rate')} | MRR {s.get('mrr')}")
        return 0

    # ---- 回归比对模式 ----
    b_path = BASELINE_DIR / f"{args.baseline}.json"
    c_path = Path(args.current) if args.current else default_current

    if not b_path.exists():
        print(f"[比对] 基线不存在: {b_path}\n       先执行: python -m eval.compare --snapshot", file=sys.stderr)
        return 1
    if not c_path.exists():
        print(f"[比对] 当前结果不存在: {c_path}\n       先执行: python -m eval --retrieval-only", file=sys.stderr)
        return 1

    baseline = _load(b_path)
    current = _load(c_path)
    has_reg, regs, imps = compare(baseline, current)

    print("=" * 70)
    print(f"📊 回归比对: 基线[{args.baseline}] vs 当前[{c_path.name}]")
    print("=" * 70)

    bs, cs = baseline.get("summary", {}), current.get("report", {}).get("summary", {})
    print(f"{'指标':<16}{'基线':>12}{'当前':>12}{'判定':>10}")
    for key, label in (("pass_rate", "通过率"), ("recall_at_k_rate", "Recall@K"), ("mrr", "MRR")):
        bv, cv = bs.get(key, 0), cs.get(key, 0)
        verdict = "✅ 持平" if abs(cv - bv) <= _EPS else ("✅ 提升" if cv > bv else "❌ 下降")
        print(f"{label:<16}{bv:>12.4f}{cv:>12.4f}{verdict:>10}")

    if imps:
        print(f"\n🎉 改善 {len(imps)} 项:")
        for i in imps[:20]:
            print(f"   + {i}")
    if regs:
        print(f"\n❌ 回归 {len(regs)} 项:")
        for r in regs[:30]:
            print(f"   - {r}")
        print("\n" + "=" * 70)
        print("结论: 检测到回归 —— 禁止继续下一步，先修复或回退")
        print("=" * 70)
        return 1

    print("\n" + "=" * 70)
    print("结论: 无回归 ✅ 可以继续下一步")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
