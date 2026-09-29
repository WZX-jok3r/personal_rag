"""
Conversation Session Manager

职责：

管理用户会话状态。

包含：

- session创建
- message保存
- history获取
- TTL控制
- 最大上下文长度控制


后续升级：

Redis:
    保存session

PostgreSQL:
    保存conversation/message

当前:
    内存版本
"""

import time
import uuid
import threading

from typing import List, Dict, Optional


class SessionStore:

    def __init__(
            self,
            ttl_seconds: int = 3600,
            max_history: int = 20
    ):

        """
        Args:

            ttl_seconds:
                session过期时间


            max_history:
                最大保存消息数量

        """

        self.ttl_seconds = ttl_seconds

        self.max_history = max_history

        self.sessions = {}

        self.lock = threading.Lock()

    def create_session(self) -> str:

        """
        创建新的session
        """

        session_id = str(
            uuid.uuid4()
        )

        with self.lock:
            self.sessions[
                session_id
            ] = {

                "messages": [],

                "created_at":
                    time.time(),

                "updated_at":
                    time.time()

            }

        return session_id

    def exists(
            self,
            session_id: str
    ) -> bool:

        """
        判断session是否存在
        """

        with self.lock:
            return (
                    session_id
                    in self.sessions
            )

    def add_message(
            self,
            session_id: str,
            role: str,
            content: str
    ):

        """
        添加消息

        role:

        user
        assistant

        """

        with self.lock:

            if (
                    session_id
                    not in self.sessions
            ):
                self.create_session()

            session = (
                self.sessions[
                    session_id
                ]
            )

            session["messages"].append(
                {

                    "role": role,

                    "content": content,

                    "timestamp":
                        time.time()

                }
            )

            # 保留最近N条

            if (
                    len(
                        session["messages"]
                    )
                    >
                    self.max_history
            ):
                session["messages"] = (
                    session["messages"]
                    [
                        -self.max_history:
                    ]
                )

            session["updated_at"] = (
                time.time()
            )

    def get_history(
            self,
            session_id: str
    ) -> List[Dict]:

        """
        获取历史消息
        """

        with self.lock:
            session = (
                self.sessions.get(
                    session_id
                )
            )

            if not session:
                return []

            # 更新访问时间

            session["updated_at"] = (
                time.time()
            )

            return (
                session["messages"]
                .copy()
            )

    def clear(
            self,
            session_id: str
    ):

        """
        删除session
        """

        with self.lock:
            if session_id in self.sessions:
                del self.sessions[
                    session_id
                ]

    def cleanup_expired(self):

        """
        清理过期session
        """

        now = time.time()

        expired = []

        with self.lock:

            for (
                    session_id,
                    session
            ) in self.sessions.items():

                if (
                        now -
                        session["updated_at"]
                        >
                        self.ttl_seconds
                ):
                    expired.append(
                        session_id
                    )

            for session_id in expired:
                del self.sessions[
                    session_id
                ]

        return expired
