"""RAG 离线评测包（迁移自 src/eval_runner.py）。

用法（cwd 切到 backend/）：
    python -m eval --retrieval-only        # 仅测检索指标（确定性，无 LLM）
    python -m eval --top-k 5 --output eval_result.json
"""
