"""会话管理路由：POST /api/v1/sessions 新建，DELETE /api/v1/sessions/{id} 删除。

会话持久化在 PG（事实来源）+ Redis（热缓存），所有操作带 tenant ACL。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_principal, get_session_service
from app.core.exceptions import NotFoundError
from app.core.security import Principal
from app.schemas.chat import SessionCreated
from app.services.session_service import SessionService

router = APIRouter(tags=["sessions"])


@router.post("/sessions", response_model=SessionCreated, summary="新建空白会话")
async def create_session(
    principal: Principal = Depends(get_principal),
    svc: SessionService = Depends(get_session_service),
) -> SessionCreated:
    session_id = await svc.create(principal.tenant_id)
    return SessionCreated(session_id=session_id)


@router.delete("/sessions/{session_id}", summary="清空/删除会话")
async def delete_session(
    session_id: str,
    principal: Principal = Depends(get_principal),
    svc: SessionService = Depends(get_session_service),
) -> dict:
    removed = await svc.delete(session_id, principal.tenant_id)
    if not removed:
        raise NotFoundError(f"session {session_id} 不存在")
    return {"deleted": session_id}
