"""会话与消息仓储（含多租户 ACL 判定）。"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import func, select

from app.models.chat import ChatSession, Message
from app.repositories.base import BaseRepository


class SessionRepository(BaseRepository):
    # ---------- 会话 ----------
    async def create(self, session_id: str, tenant_id: Optional[str]) -> ChatSession:
        sess = ChatSession(id=session_id, tenant_id=tenant_id)
        self._session.add(sess)
        await self.flush()
        return sess

    async def get(self, session_id: str) -> Optional[ChatSession]:
        return await self._session.get(ChatSession, session_id)

    async def exists(self, session_id: str, tenant_id: Optional[str]) -> bool:
        """ACL：会话必须属于同一租户（严格相等）。鉴权关闭时双方均为 None，正常匹配。"""
        stmt = select(ChatSession.id).where(
            ChatSession.id == session_id,
            ChatSession.tenant_id == tenant_id,
        )
        return (await self._session.execute(stmt)).first() is not None

    async def touch(self, session_id: str) -> None:
        """刷新 last_active_at（读取/写入即续期）。"""
        await self._session.execute(
            ChatSession.__table__.update()
            .where(ChatSession.id == session_id)
            .values(last_active_at=func.now())
        )

    async def delete(self, session_id: str, tenant_id: Optional[str]) -> bool:
        """按 (id, tenant) 精确删除，命中返回 True。CASCADE 连带删消息。"""
        sess = await self.get(session_id)
        if sess is None or sess.tenant_id != tenant_id:
            return False
        await self._session.delete(sess)
        await self.flush()
        return True

    # ---------- 消息 ----------
    async def next_seq(self, session_id: str) -> int:
        stmt = select(func.coalesce(func.max(Message.seq), 0)).where(
            Message.session_id == session_id
        )
        current = (await self._session.execute(stmt)).scalar_one()
        return int(current) + 1

    async def add_message(self, session_id: str, role: str, content: str, seq: int) -> Message:
        msg = Message(session_id=session_id, role=role, content=content, seq=seq)
        self._session.add(msg)
        await self.flush()
        return msg

    async def get_recent_messages(self, session_id: str, limit: int) -> List[Message]:
        """取最近 limit 条并按 seq 升序返回（用于回源重建历史）。"""
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.seq.desc())
            .limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        rows.reverse()  # 降序取最近 N 条后反转为时间正序
        return rows
