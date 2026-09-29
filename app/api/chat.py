"""
Chat API

职责:

提供聊天接口

包括:

POST /chat

POST /chat/stream


不负责:

- session实现
- RAG逻辑
- LLM调用


"""

import json
import logging

from fastapi import (
    APIRouter,
    Request
)

from fastapi.responses import (
    StreamingResponse
)

from app.chat.service import (
    ChatService
)

router = APIRouter(
    prefix="/api/v1",
    tags=["chat"]
)

logger = logging.getLogger(__name__)

chat_service = ChatService()


@router.post(
    "/chat"
)
async def chat(
        request: Request
):
    """
    普通聊天


    Request:

    {
        session_id:"",
        message:"",
        top_k:10
    }

    """

    body = await request.json()

    session_id = (
        body.get(
            "session_id"
        )
    )

    message = (
        body.get(
            "message"
        )
    )

    top_k = (
        body.get(
            "top_k",
            10
        )
    )

    result = (
        chat_service.chat(
            session_id=session_id,
            message=message,
            top_k=top_k
        )
    )

    return result


@router.post(
    "/chat/stream"
)
async def chat_stream(
        request: Request
):
    """
    SSE流式聊天
    """

    body = await request.json()

    session_id = (
        body.get(
            "session_id"
        )
    )

    message = (
        body.get(
            "message"
        )
    )

    top_k = (
        body.get(
            "top_k",
            10
        )
    )

    async def event_generator():
        for item in (
                chat_service.stream_chat(
                    session_id=session_id,
                    message=message,
                    top_k=top_k
                )
        ):
            yield (
                    "data: "
                    +
                    json.dumps(
                        item,
                        ensure_ascii=False
                    )
                    +
                    "\n\n"
            )

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type=
        "text/event-stream"
    )
