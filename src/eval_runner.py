"""
eval_runner.py - 评测脚本

功能:
1. 读取 qa_test.jsonl 评测集
2. 对每个问题执行 RAG 查询
3. 计算指标:
   - Recall@K: 期望来源文件是否在前 K 个检索结果中
   - Keyword Hit Rate: LLM 回答中包含多少 expected_keywords
   - Source Match: 检索结果是否来自正确的文件
4. 输出分类统计报告

用法:
    python src/eval_runner.py              # 运行全部评测
    python src/eval_runner.py --top-k 10   # 指定 K 值
    python src/eval_runner.py --output result_v1.json  # 保存详细结果
"""

import json
import argparse
import logging
from pathlib import Path
from typing import List, Dict, Any, Tuple
from collections import defaultdict

from config import TEST_DATASET_FILE
from rag_pipeline import get_pipeline, RAGPipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 评测输出统一目录
OUTPUT_EVAL_DIR = Path("output")


def dump_text_report(report: Dict[str, Any], txt_path: Path):
    """把评测报告写入txt文件"""
    s = report["summary"]
    lines = []
    lines.append("=" * 60)
    lines.append("📊 RAG 评测报告")
    lines.append("=" * 60)
    lines.append(f"总问题数: {s['total_questions']}")
    lines.append(f"通过: {s['passed']} | 失败: {s['failed']}")
    lines.append(f"通过率: {s['pass_rate'] * 100:.2f}%")
    lines.append(f"Recall@K 命中率: {s['recall_at_k_rate'] * 100:.2f}% (K={s['top_k']})")
    lines.append(f"平均关键词命中率: {s['avg_keyword_hit_rate'] * 100:.2f}%")
    lines.append("=" * 60)
    lines.append("")

    lines.append("📁 按来源文件统计:")
    for src, stats in report["by_source"].items():
        lines.append(
            f"  {src}: 通过 {stats['passed']}/{stats['total']} (Recall命中 {stats['recall_hits']}/{stats['total']})")
    lines.append("")

    lines.append("📂 按类别统计:")
    for cat, stats in report["by_category"].items():
        lines.append(
            f"  {cat}: 通过 {stats['passed']}/{stats['total']}, 平均关键词命中率 {stats['avg_keyword_hit'] * 100:.2f}%")
    lines.append("")

    if report["failed_cases"]:
        lines.append("❌ 失败案例:")
        for i, fc in enumerate(report["failed_cases"], 1):
            lines.append(f"  {i}. [{fc['expected_source']}] {fc['question'][:80]}...")
            lines.append(f"     Recall@K={fc['recall_at_k']}, KeywordHit={fc['keyword_hit_rate'] * 100:.1f}%")
            lines.append(f"     回答: {fc['actual_answer'][:150]}...")
            lines.append("")

    lines.append("=" * 60)
    content = "\n".join(lines)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(content)


class EvalRunner:
    """评测执行器"""

    def __init__(self, pipeline: RAGPipeline, top_k: int = 5):
        self.pipeline = pipeline
        self.top_k = top_k

    def load_dataset(self, dataset_path: Path) -> List[Dict[str, Any]]:
        """加载 JSONL 评测集"""
        questions = []
        with open(dataset_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    questions.append(item)
                except json.JSONDecodeError as e:
                    logger.warning(f"[Eval] 解析 JSONL 失败: {e}")
        logger.info(f"[Eval] 加载评测集: {len(questions)} 条")
        return questions

    def evaluate_single(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        评测单条数据
        Returns: 包含原始数据 + 评测结果的字典
        """
        question = item["question"]
        golden_answer = item.get("golden_answer", "")
        expected_source = item.get("source_file", "")
        expected_keywords = item.get("expected_keywords", [])

        logger.info(f"[Eval] 评测: {question[:50]}...")

        # 执行 RAG 查询
        result = self.pipeline.query(question, top_k=self.top_k)
        actual_answer = result["answer"]
        sources = result["sources"]

        # 添加字符标准化函数
        def normalize_source_name(source_str):
            """标准化source名称，将各种破折号替换为标准短横线"""
            if not source_str:
                return source_str
            return source_str.replace("‑", "-").replace("–", "-").replace("—", "-")

            # 1. 计算 Recall@K

        retrieved_sources = [s["source"] for s in sources]

        # 标准化source名称
        normalized_retrieved_sources = [normalize_source_name(src) for src in retrieved_sources]
        normalized_expected_source = normalize_source_name(expected_source) if expected_source else None

        recall_at_k = normalized_expected_source in normalized_retrieved_sources if normalized_expected_source else None

        # 2. 计算 Source Rank: 期望来源在结果中的排名（1‑based，未找到为 -1）
        source_rank = -1
        if expected_source:
            for i, src in enumerate(retrieved_sources):
                if src == expected_source:
                    source_rank = i + 1
                    break

        # 3. 计算 Keyword Hit Rate: 回答中包含多少期望关键词
        answer_lower = actual_answer.lower()
        keyword_hits = []
        for kw in expected_keywords:
            # 支持模糊匹配：去除空格和特殊字符后比较
            kw_clean = kw.lower().replace(" ", "").replace("‑", "-")
            ans_clean = answer_lower.replace(" ", "").replace("‑", "-")
            hit = kw_clean in ans_clean
            keyword_hits.append({"keyword": kw, "hit": hit})

        keyword_hit_rate = sum(1 for h in keyword_hits if h["hit"]) / len(keyword_hits) if keyword_hits else 0

        # 4. 综合评分
        # 如果 Recall@K 为 True 且 Keyword Hit Rate >= 0.6，认为通过
        passed = recall_at_k is True and keyword_hit_rate >= 0.6

        return {
            # 原始数据
            "question": question,
            "golden_answer": golden_answer,
            "expected_source": expected_source,
            "expected_keywords": expected_keywords,
            "category": item.get("category", "未分类"),

            # RAG 结果
            "actual_answer": actual_answer,
            "retrieved_sources": retrieved_sources,
            "retrieved_count": result["retrieved_count"],

            # 评测指标
            "recall_at_k": recall_at_k,
            "source_rank": source_rank,
            "keyword_hits": keyword_hits,
            "keyword_hit_rate": round(keyword_hit_rate, 4),
            "passed": passed,
        }

    def run(self, dataset_path: Path) -> Dict[str, Any]:
        """
        执行完整评测
        Returns: 评测报告
        """
        questions = self.load_dataset(dataset_path)
        if not questions:
            logger.error("[Eval] 评测集为空")
            return {}

        results = []
        for item in questions:
            try:
                eval_result = self.evaluate_single(item)
                results.append(eval_result)
            except Exception as e:
                logger.error(f"[Eval] 评测失败 [{item.get('question', 'unknown')}]: {e}")
                results.append({
                    "question": item.get("question", ""),
                    "error": str(e),
                    "passed": False,
                })

        # 生成报告
        report = self._generate_report(results)
        return {"results": results, "report": report}

    def _generate_report(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """生成评测报告"""
        total = len(results)
        passed = sum(1 for r in results if r.get("passed", False))
        failed = total - passed

        # 整体指标
        recall_hits = [r for r in results if r.get("recall_at_k") is True]
        recall_misses = [r for r in results if r.get("recall_at_k") is False]
        recall_none = [r for r in results if r.get("recall_at_k") is None]

        avg_keyword_hit = sum(
            r.get("keyword_hit_rate", 0) for r in results if "keyword_hit_rate" in r) / total if total > 0 else 0

        # 按 source_file 分类统计
        by_source = defaultdict(lambda: {"total": 0, "passed": 0, "recall_hits": 0})
        for r in results:
            src = r.get("expected_source", "unknown")
            by_source[src]["total"] += 1
            if r.get("passed"):
                by_source[src]["passed"] += 1
            if r.get("recall_at_k") is True:
                by_source[src]["recall_hits"] += 1

        # 按 category 分类统计
        by_category = defaultdict(lambda: {"total": 0, "passed": 0, "avg_keyword_hit": []})
        for r in results:
            cat = r.get("category", "未分类")
            by_category[cat]["total"] += 1
            if r.get("passed"):
                by_category[cat]["passed"] += 1
            if "keyword_hit_rate" in r:
                by_category[cat]["avg_keyword_hit"].append(r["keyword_hit_rate"])

        # 计算分类平均
        for cat in by_category:
            hits = by_category[cat]["avg_keyword_hit"]
            by_category[cat]["avg_keyword_hit"] = round(sum(hits) / len(hits), 4) if hits else 0

        # 失败案例
        failed_cases = [r for r in results if not r.get("passed", False)]

        report = {
            "summary": {
                "total_questions": total,
                "passed": passed,
                "failed": failed,
                "pass_rate": round(passed / total, 4) if total > 0 else 0,
                "recall_at_k_rate": round(len(recall_hits) / (len(recall_hits) + len(recall_misses)), 4) if (
                                                                                                                    len(recall_hits) + len(
                                                                                                                recall_misses)) > 0 else 0,
                "avg_keyword_hit_rate": round(avg_keyword_hit, 4),
                "top_k": self.top_k,
            },
            "by_source": dict(by_source),
            "by_category": dict(by_category),
            "failed_cases": [
                {
                    "question": r["question"],
                    "expected_source": r.get("expected_source", ""),
                    "recall_at_k": r.get("recall_at_k"),
                    "keyword_hit_rate": r.get("keyword_hit_rate", 0),
                    "actual_answer": r.get("actual_answer", "")[:200],
                }
                for r in failed_cases
            ],
        }

        return report

    def print_report(self, report: Dict[str, Any]):
        """打印评测报告到控制台"""
        s = report["summary"]

        print("\n" + "=" * 60)
        print("📊 RAG 评测报告")
        print("=" * 60)
        print(f"总问题数: {s['total_questions']}")
        print(f"通过: {s['passed']} | 失败: {s['failed']}")
        print(f"通过率: {s['pass_rate'] * 100:.2f}%")
        print(f"Recall@K 命中率: {s['recall_at_k_rate'] * 100:.2f}% (K={s['top_k']})")
        print(f"平均关键词命中率: {s['avg_keyword_hit_rate'] * 100:.2f}%")
        print("=" * 60)

        print("\n📁 按来源文件统计:")
        for src, stats in report["by_source"].items():
            print(
                f"  {src}: 通过 {stats['passed']}/{stats['total']} (Recall命中 {stats['recall_hits']}/{stats['total']})")

        print("\n📂 按类别统计:")
        for cat, stats in report["by_category"].items():
            print(
                f"  {cat}: 通过 {stats['passed']}/{stats['total']}, 平均关键词命中率 {stats['avg_keyword_hit'] * 100:.2f}%")

        if report["failed_cases"]:
            print("\n❌ 失败案例:")
            for i, fc in enumerate(report["failed_cases"][:5], 1):
                print(f"  {i}. [{fc['expected_source']}] {fc['question'][:60]}...")
                print(f"     Recall@K={fc['recall_at_k']}, KeywordHit={fc['keyword_hit_rate'] * 100:.1f}%")
                print(f"     回答: {fc['actual_answer'][:100]}...")

        print("\n" + "=" * 60)


def main():
    parser = argparse.ArgumentParser(description="RAG 评测脚本")
    default_dataset_path = Path(TEST_DATASET_FILE).with_suffix(".jsonl")
    parser.add_argument("--dataset", type=str, default=str(default_dataset_path),
                        help="评测集路径 (JSONL 格式)")
    parser.add_argument("--top-k", type=int, default=5, help="检索 Top‑K")
    # 这里只传文件名，不要文件夹路径
    parser.add_argument("--output", type=str, default="eval_result.json", help="输出文件名，保存到 output/eval/")
    args = parser.parse_args()

    # 自动创建输出目录
    OUTPUT_EVAL_DIR.mkdir(parents=True, exist_ok=True)

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        # 尝试 .json 后缀
        json_path = dataset_path.with_suffix(".json")
        if json_path.exists():
            dataset_path = json_path
        else:
            logger.error(f"[Eval] 评测集不存在: {dataset_path}")
            return

    # 初始化
    logger.info("[Eval] 初始化 RAG Pipeline...")
    pipeline = get_pipeline()
    runner = EvalRunner(pipeline, top_k=args.top_k)

    # 执行评测
    logger.info(f"[Eval] 开始评测 (Top‑K={args.top_k})...")
    eval_result = runner.run(dataset_path)

    if not eval_result:
        return

    # 打印报告到控制台
    runner.print_report(eval_result["report"])

    # 拼接完整输出路径
    output_path = OUTPUT_EVAL_DIR / args.output
    txt_output_path = output_path.with_suffix(".txt")

    # 保存json
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(eval_result, f, ensure_ascii=False, indent=2)
    logger.info(f"[Eval] JSON详细结果已保存: {output_path}")

    # 保存txt报告
    dump_text_report(eval_result["report"], txt_output_path)
    logger.info(f"[Eval] TXT评测报告已保存: {txt_output_path}")


if __name__ == "__main__":
    main()
