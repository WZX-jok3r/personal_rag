"""
根路由聚合

统一挂载 /v1 前缀；main.py 再套 /api，最终对外路径为 /api/v1/*。
业务路由：health / query / chat / sessions / documents / agent。
"""

from fastapi import APIRouter

from app.api.v1 import agent, chat, documents, health, query, sessions

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router)
api_router.include_router(query.router)
api_router.include_router(chat.router)
api_router.include_router(sessions.router)
api_router.include_router(documents.router)
# Agent 路由刻意独立于 chat：既有 /chat 与 /chat/stream 的契约已被冻结，
# 新增能力应并列而非侵入（旧前端不受影响）。
api_router.include_router(agent.router)
