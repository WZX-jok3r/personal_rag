"""LLM 用量记账（P7）：把每次模型调用的 token 与耗时落库。

## 为什么 Agent 项目必须做这件事

一次普通问答 = 1~3 次 LLM 调用；Agent 多步循环下可达 8 次（我们的 MAX_LLM_CALLS 上界）。
没有记账就意味着：
- **成本不可见**：不知道一次问答花多少 token、钱花在哪；
- **无法定容量**：不知道高峰期会打多少量；
- **无法优化**：不知道是 prompt 太长还是步数太多导致贵。

实测便利条件（改造方案 4.2 节已核实）：SiliconFlow 的流式响应**每一帧都带累计 usage**，
因此采集是零成本的，只需取最后一帧。

## 设计取舍

- 与 sql_audit 同库（系统元数据库 rag），走 Alembic。
- **只记数值与模型名，不记 prompt/回答全文** —— 全文可能含敏感信息，
  且体积大；排查问题有 Langfuse trace 就够了（本项目已有软埋点）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LlmUsage(Base):
    """一次 LLM 调用的用量记录。"""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 调用场景：chat_with_tools / text2sql_generate / answer_compose / summarize ...
    scene: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    actor: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LlmUsage scene={self.scene!r} tokens={self.total_tokens}>"
