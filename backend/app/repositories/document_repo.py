"""文档登记仓储。"""

from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy import select

from app.models.document import Document, DocumentStatus
from app.repositories.base import BaseRepository


class DocumentRepository(BaseRepository):
    async def get_by_hash(self, source: str, file_hash: str) -> Optional[Document]:
        stmt = select(Document).where(
            Document.source == source, Document.file_hash == file_hash
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_by_tenant(self, tenant_id: Optional[str]) -> List[Document]:
        stmt = select(Document).where(Document.tenant_id == tenant_id).order_by(Document.id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def upsert(
        self,
        source: str,
        file_hash: str,
        fmt: str,
        tenant_id: Optional[str],
        chunk_count: int = 0,
        status: str = DocumentStatus.READY.value,
        meta: Optional[dict] = None,
    ) -> Tuple[Document, bool]:
        """按 (source, file_hash) 建或更新。返回 (document, created?)。"""
        doc = await self.get_by_hash(source, file_hash)
        if doc is None:
            doc = Document(
                source=source, file_hash=file_hash, format=fmt, tenant_id=tenant_id,
                chunk_count=chunk_count, status=status, meta=meta or {},
            )
            self._session.add(doc)
            await self.flush()
            return doc, True
        doc.format = fmt
        doc.tenant_id = tenant_id
        doc.chunk_count = chunk_count
        doc.status = status
        doc.meta = meta or {}
        await self.flush()
        return doc, False
