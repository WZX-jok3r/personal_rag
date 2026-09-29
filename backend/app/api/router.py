"""
根路由聚合

统一挂载 /v1 前缀；main.py 再套 /api，最终对外路径为 /api/v1/*。
新增业务路由（chat / query / documents / knowledge / sessions / auth）
在后续阶段逐个 include 进来。
"""

from fastapi import APIRouter

from app.api.v1 import health

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router)
