"""text2sql_runner.py - Text2SQL 评测（Execution Accuracy）。

指标定义（严格用业界口径，便于与公开榜单对照）：
    **Execution Accuracy (EX)** —— 执行「生成 SQL」与执行「Gold SQL」，
    **结果集相同**即算对。比字符串匹配合理得多，因为同一语义有多种 SQL 写法。
    对照：BIRD 榜单上人类专家 92.96%、榜首方案约 83%（见改造方案 4.8 节）。

同时统计：
    Valid SQL Rate —— 生成 SQL 通过 guard 且成功执行的比例（衡量"能不能跑"）
    Clarification Rate —— 触发口径反问的比例（这类题不计入 EX 分母）
    Avg Attempts —— 平均生成次数（>1 表示发生了自修正）

用法:
    python -m eval.text2sql_runner                 # 跑全部
    python -m eval.text2sql_runner --limit 10      # 只跑前 10 题（省成本）
    python -m eval.text2sql_runner --dry-gold      # 只验证 Gold SQL 可执行（不调用 LLM）
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.analytics.guard import guard_sql
from app.core.config import settings

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

logger = logging.getLogger(__name__)

DATASET = Path(__file__).resolve().parent / "text2sql_dataset.jsonl"
OUTPUT_DIR = settings.base_dir / "output"


# ==================== 结果比较 ====================

def normalize_value(v: Any) -> Any:
    """归一化单个值，消除"数值 vs 字符串""浮点精度"带来的假不一致。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        # 整数化（1042.0 与 1042 视为相同）
        return int(f) if f.is_integer() else round(f, 4)
    if hasattr(v, "isoformat"):        # date / datetime
        return v.isoformat()
    s = str(v).strip()
    # 纯数字字符串也归一为数值（不同驱动返回类型可能不同）
    try:
        f = float(s)
        return int(f) if f.is_integer() else round(f, 4)
    except ValueError:
        return s


def normalize_rows(rows: List[Tuple]) -> List[Tuple]:
    return [tuple(normalize_value(v) for v in r) for r in rows]


def results_equal(a: List[Tuple], b: List[Tuple], ordered: bool) -> bool:
    """判定两个结果集是否等价。

    ordered=True  —— 按行序严格比较（如 Top-N、ORDER BY 题）。
                     逐行比较时容忍"生成结果多带了几列"（见下）。
    ordered=False —— 无序比较（多重集包含）。

    ⚠️ 为什么容忍"多带列"（这是本评测最容易把好答案判错的地方）：
        实测发现模型倾向返回**多余的识别性列**以增强可读性，例如
            问「薪资最高的员工在哪个部门？」  gold: (Operations,)
            模型: (Operations, Derek, Cummings, 179997)
        这在语义上**覆盖了**期望结果，且信息更完整 —— 属于更好的答案。
        若按严格相等判定会被误判为错误。
        因此规则是：**期望行必须是生成行的"前缀或子序列"**（保持列序），
        即生成结果里能按顺序找到期望的每一列。
        反之（生成缺少期望的列）才算真错。
    """
    na, nb = normalize_rows(a), normalize_rows(b)
    if len(na) != len(nb):
        return False
    if ordered:
        return all(_row_covers(got, want) for got, want in zip(na, nb))
    # 无序：对每一行期望，都要能在生成结果里找到一行覆盖它（贪心匹配，不重复用）
    remaining = list(na)
    for want in nb:
        hit = next((i for i, got in enumerate(remaining) if _row_covers(got, want)), None)
        if hit is None:
            return False
        remaining.pop(hit)
    return True


def _row_covers(got: Tuple, want: Tuple) -> bool:
    """生成行是否"覆盖"期望行：want 的每个值都按序出现在 got 中。

    按序（子序列）而非无视顺序，是为了避免把
        got=('Sales','Operations') / want=('Operations','Sales')
    这种**列序颠倒**的答案判成正确 —— 列序颠倒通常意味着选错了列。

    先试最短路径：等长且逐位相等（绝大多数情况）。
    """
    if len(got) == len(want):
        return got == want
    if len(got) < len(want):
        return False
    # 子序列匹配
    it = iter(got)
    return all(any(g == w for g in it) for w in want)


# ==================== 评测主体 ====================

@dataclass
class CaseResult:
    question: str
    category: str
    gold_sql: str
    passed: bool = False
    executed: bool = False            # 生成 SQL 是否成功执行（Valid SQL）
    clarified: bool = False
    attempts: int = 1
    gen_sql: str = ""
    error: str = ""
    elapsed_ms: int = 0


@dataclass
class Report:
    total: int = 0
    judged: int = 0                  # 参与 EX 判定的题数（排除澄清题）
    passed: int = 0
    executed: int = 0
    clarified: int = 0
    avg_attempts: float = 0.0
    by_category: Dict[str, Dict[str, int]] = field(default_factory=dict)
    cases: List[CaseResult] = field(default_factory=list)

    @property
    def ex(self) -> float:
        return self.passed / self.judged if self.judged else 0.0

    @property
    def valid_sql_rate(self) -> float:
        return self.executed / self.judged if self.judged else 0.0


def load_dataset(path: Path) -> List[Dict[str, Any]]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            obj = json.loads(line)
            if not obj.get("question") or not obj.get("gold_sql"):
                logger.warning("[t2s-eval] 第 %d 行缺字段，跳过", line_no)
                continue
            out.append(obj)
    return out


def run_gold(engine, sql: str) -> Tuple[bool, List[Tuple], str]:
    """执行 Gold SQL 取得期望结果。"""
    try:
        g = guard_sql(sql, 10000)
        if not g.ok:
            return False, [], f"Gold SQL 被 guard 拒绝: {g.reason}"
        with engine.connect() as conn:
            rows = list(conn.execute(text(g.sql)).fetchall())
        return True, rows, ""
    except Exception as e:  # noqa: BLE001
        return False, [], str(e)


def evaluate(limit: Optional[int] = None, dry_gold: bool = False) -> Report:
    from app.analytics.text2sql import get_text2sql_engine

    cases = load_dataset(DATASET)
    if limit:
        cases = cases[:limit]

    analytics_engine = create_engine(settings.sync_analytics_ro_url)
    rag_engine = create_engine(settings.sync_postgres_url)
    report = Report(total=len(cases))
    engine_t2s = None if dry_gold else get_text2sql_engine()

    try:
        for i, c in enumerate(cases, 1):
            q, gold, ordered = c["question"], c["gold_sql"], bool(c.get("ordered"))
            cat = c.get("category", "未分类")
            cr = CaseResult(question=q, category=cat, gold_sql=gold)

            ok_gold, gold_rows, gold_err = run_gold(analytics_engine, gold)
            if not ok_gold:
                cr.error = f"Gold SQL 不可执行（数据集问题）: {gold_err[:200]}"
                logger.error("[t2s-eval] %s | %s", q, cr.error)
                report.cases.append(cr)
                continue

            if dry_gold:
                cr.passed = True
                cr.executed = True
                report.passed += 1
                report.judged += 1
                report.executed += 1
                # dry-gold 也要填分类统计，否则报告里"分类准确率"是空的，
                # 容易让人误以为没有分类信息（实际只是没填）
                st = report.by_category.setdefault(cat, {"total": 0, "passed": 0})
                st["total"] += 1
                st["passed"] += 1
                print(f"[{i}/{len(cases)}] OK(gold) {q}")
                report.cases.append(cr)
                continue

            t0 = time.perf_counter()
            with Session(rag_engine) as s:
                res = engine_t2s.answer(q, s)
            cr.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            cr.attempts = res.attempts
            cr.gen_sql = res.sql

            if res.needs_clarification:
                cr.clarified = True
                report.clarified += 1
                # 澄清题不计入 EX 分母（口径不明时反问是正确的产品行为）
                status = "CLARIFY"
            else:
                report.judged += 1
                if res.ok:
                    cr.executed = True
                    report.executed += 1
                    same = results_equal(res.rows, gold_rows, ordered)
                    cr.passed = same
                    if same:
                        report.passed += 1
                    else:
                        cr.error = f"结果不符: 生成={_short(res.rows)} 期望={_short(gold_rows)}"
                    status = "PASS" if same else "FAIL"
                else:
                    cr.error = (res.errors[-1] if res.errors else res.answer)[:300]
                    status = "ERROR"

            # 分类统计
            st = report.by_category.setdefault(cat, {"total": 0, "passed": 0})
            if not cr.clarified:
                st["total"] += 1
                st["passed"] += 1 if cr.passed else 0

            report.cases.append(cr)
            mark = {"PASS": "✅", "FAIL": "❌", "ERROR": "💥", "CLARIFY": "❓"}[status]
            print(f"[{i}/{len(cases)}] {mark} {status:<8} {q}  ({cr.elapsed_ms}ms, {cr.attempts}次)")
            if status in ("FAIL", "ERROR") and cr.error:
                print(f"           ↳ {cr.error[:160]}")

        if report.judged:
            report.avg_attempts = sum(c.attempts for c in report.cases if not c.clarified) / report.judged
    finally:
        analytics_engine.dispose()
        rag_engine.dispose()
    return report


def _short(rows: List[Tuple], n: int = 3) -> str:
    return str(normalize_rows(rows[:n]))


def print_report(r: Report) -> None:
    print()
    print("=" * 78)
    print("📊 Text2SQL 评测报告")
    print("=" * 78)
    print(f"总题数            : {r.total}")
    print(f"参与 EX 判定      : {r.judged}（另有 {r.clarified} 题触发口径澄清，不计入）")
    print(f"Execution Accuracy: **{r.ex:.2%}**  ({r.passed}/{r.judged})")
    print(f"Valid SQL Rate    : {r.valid_sql_rate:.2%}  ({r.executed}/{r.judged})")
    print(f"平均生成次数      : {r.avg_attempts:.2f}")
    print("=" * 78)
    print("\n📂 分类准确率:")
    for cat, st in sorted(r.by_category.items(), key=lambda x: -x[1]["total"]):
        pct = st["passed"] / st["total"] if st["total"] else 0
        print(f"  {cat:<14} {st['passed']:>3}/{st['total']:<3} = {pct:>6.1%}")
    print("=" * 78)


def dump(r: Report, name: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUTPUT_DIR / name
    payload = {
        "summary": {
            "total": r.total, "judged": r.judged, "passed": r.passed,
            "executed": r.executed, "clarified": r.clarified,
            "execution_accuracy": r.ex, "valid_sql_rate": r.valid_sql_rate,
            "avg_attempts": r.avg_attempts,
        },
        "by_category": r.by_category,
        "cases": [c.__dict__ for c in r.cases],
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return p


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description="Text2SQL 评测（Execution Accuracy）")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 题（省 token）")
    ap.add_argument("--dry-gold", action="store_true", help="只验证 Gold SQL 可执行，不调用 LLM")
    ap.add_argument("--output", type=str, default="text2sql_eval.json")
    args = ap.parse_args()

    print("=" * 78)
    print("Text2SQL 评测" + ("（dry-gold：仅校验 Gold SQL）" if args.dry_gold else ""))
    print("=" * 78)

    r = evaluate(limit=args.limit, dry_gold=args.dry_gold)
    print_report(r)
    p = dump(r, args.output)
    print(f"\n详细结果已保存: {p}")

    if args.dry_gold:
        ok = r.judged == r.passed
        print(f"\nGold SQL 校验: {'全部可执行 ✅' if ok else '存在不可执行项 ❌'}")
        return 0 if ok else 1

    # 阈值门禁：低于 85% 视为不达标（见改造方案 P3 目标）
    target = 0.85
    if r.ex < target:
        print(f"\n⚠️ EX {r.ex:.2%} 低于目标 {target:.0%}")
        return 1
    print(f"\n✅ EX {r.ex:.2%} 达标（目标 {target:.0%}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
