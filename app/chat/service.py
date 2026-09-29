"""
Chat Service


职责:

处理完整聊天流程:


Session

↓

Query Rewrite

↓

RAG Pipeline

↓

保存消息



"""

import logging

from typing import Dict, Any

from app.rag.pipeline import (
    RAGPipeline
)

from app.rag.query_rewriter import (
    QueryRewriter
)

from .session import (
    SessionStore
)

logger = logging.getLogger(__name__)


class ChatService:

    def __init__(
            self,
            rag_pipeline=None,
            session_store=None
    ):
        self.rag = (
            rag_pipeline
            if rag_pipeline
            else RAGPipeline()
        )

        self.sessions = (
            session_store
            if session_store
            else SessionStore()
        )

        # 查询改写器

        self.query_rewriter = (
            QueryRewriter()
        )

    def chat(
            self,
            session_id: str,
            message: str,
            top_k: int = 10
    ) -> Dict[str, Any]:
        """
        普通聊天


        """

        logger.info(
            f"[CHAT] session={session_id}"
        )

        #
        # 1.
        # 获取历史
        #

        history = (
            self.sessions.get_history(
                session_id
            )
        )

        #
        # 2.
        # 保存用户问题
        #

        self.sessions.add_message(
            session_id,
            "user",
            message
        )

        #
        # 3.
        # Query Rewrite
        #

        rewritten_query = (
            self.query_rewriter.rewrite(
                query=message,
                history=history
            )
        )

        #
        # 4.
        # 调用RAG
        #

        result = (
            self.rag.query(
                query=rewritten_query,
                top_k=top_k
            )
        )

        #
        # 5.
        # 保存回答
        #

        answer = (
            result.get(
                "answer",
                ""
            )
        )

        self.sessions.add_message(
            session_id,
            "assistant",
            answer
        )

        #
        # 6.
        # 返回
        #

        return {

            # 用户原问题

            "query":
                message,

            # 实际检索问题

            "rewritten_query":
                rewritten_query,

            "answer":
                answer,

            "sources":
                result.get(
                    "sources",
                    []
                )

        }
