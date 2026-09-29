"""
Session API

职责:

管理聊天session


接口:

POST   /session/create

GET    /session/{session_id}

DELETE /session/{session_id}


"""

from fastapi import (
    APIRouter,
    HTTPException
)

from app.chat.service import (
    ChatService
)

router = APIRouter(
    prefix="/api/v1/session",
    tags=["session"]
)

chat_service = ChatService()


@router.post(
    "/create"
)
async def create_session():
    """
    创建新的聊天会话

    返回:

    {
        session_id:"xxx"
    }

    """

    session_id = (
        chat_service
        .sessions
        .create_session()
    )

    return {

        "session_id":
            session_id

    }


@router.get(
    "/{session_id}"
)
async def get_session(
        session_id: str
):
    """
    获取历史消息
    """

    history = (
        chat_service
        .sessions
        .get_history(
            session_id
        )
    )

    return {

        "session_id":
            session_id,

        "messages":
            history

    }


@router.delete(
    "/{session_id}"
)
async def delete_session(
        session_id: str
):
    """
    删除会话
    """

    chat_service.sessions.clear(
        session_id
    )

    return {

        "success":
            True

    }
