"""
Conversation Memory

当前:
内存保存


企业版:
Redis/PostgreSQL
"""

from collections import defaultdict


class ConversationMemory:

    def __init__(self):
        self.messages = (
            defaultdict(list)
        )

    def add_message(
            self,
            session_id,
            role,
            content
    ):
        self.messages[
            session_id
        ].append(
            {
                "role": role,
                "content": content
            }
        )

    def get_history(
            self,
            session_id
    ):
        return (
            self.messages
            .get(
                session_id,
                []
            )
        )

    def clear(
            self,
            session_id
    ):
        if session_id in self.messages:
            del self.messages[
                session_id
            ]
