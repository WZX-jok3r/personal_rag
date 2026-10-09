# 评测基线记录（改动的对照基准，纳入 git）

> 本文件与同目录下的 `baseline_v1.json` 配套。**任何改动检索/分块/入库/评测的提交，
> 都必须先用 `scripts/check.ps1 -Full` 与本基线逐题比对，无回归才允许继续。**

## baseline_v1 · 冻结于 HEAD `159d639`（改造开始前）

**采集条件**
- 分支 `refactor`，commit `159d639`（feat(ingestion): 为大表 xlsx 生成聚合摘要块）
- 评测口径：`python -m eval --retrieval-only`（仅检索，跳过 LLM 生成 → **确定性、可复现**）
- 评测集：`test_dataset/qa_test_v2.jsonl`（107 题，参与判定 101 题，排除 6 题 no_knowledge 拒答）
- 向量库：Qdrant `personal_rag_v2`，1402 个点
- 检索配置：`retrieval_mode=hybrid`、`top_k=5`、`prefetch_k=20`、`rerank_enabled=true`、
  `rerank_candidates=20`

**基线指标**

| 指标 | 值 |
|---|---|
| 总问题数 | 107（参与判定 101） |
| 通过 | 100 |
| 失败 | 1 |
| 通过率 | **99.01%** |
| Recall@K | **99.01%**（K=5） |
| MRR | **0.9703** |
| rank 分布 | rank_1 **96** / rank_2_3 4 / rank_4_5 0 / miss **1** |

**已知的 1 道失败题（不算回归）**
- `[docx-sample-with-table.docx] Finance部门的员工有哪些岗位？` → 未召回（Rank=-1）
- 性质：跨文件同质词面 miss，与 `159d639` 提交说明一致（"剩余 1 道为跨文件同质词面 miss"）
- **因此基线不是 100%，这 1 道题在后续比对中应始终表现为"已知 miss"，不应被当作新回归**

**代码基线**
- 业务代码 7848 行 / 81 文件
- `pytest -q` → **45 passed**（P0 新增 10 个回归闸口测试后为 **55 passed**）

## 如何使用

```powershell
# 1) 改动后跑完整验证（含评测 + 逐题比对）
.\scripts\check.ps1 -Full

# 2) 若只想手工比对
cd backend
python -m eval --retrieval-only
python -m eval.compare --baseline baseline_v1
```

**闸口判定规则**（`backend/eval/compare.py`）
- 聚合指标（通过率 / Recall@K / MRR）**不得下降**
- **逐题**比对 `recall_at_k` / `source_rank` / `passed`，任一劣化即判回归
- 为什么必须逐题：聚合指标会掩盖"等量置换"（A 题修好、B 题坏了，召回率仍 99.01%）
- 退出码 1 = 有回归 = **禁止继续下一步**

> ⚠️ **一个重要设计取舍**：当前基线是 **retrieval-only**，它**不含 LLM 生成**，
> 因此它只能守住"检索行为不退化"，**守不住"答案质量不退化"**。
> 这是刻意的——retrieval-only 是**确定性**的（同样的 query+索引→同样的结果），
> 适合做回归闸口；而带 LLM 生成的评测有随机性，不适合当门禁。
> 答案质量评测（faithfulness / LLM-as-judge）列为 P7 的独立任务。
