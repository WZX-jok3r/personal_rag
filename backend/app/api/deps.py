"""
deps.py - FastAPI 公共依赖

集中管理路由层复用的依赖项（身份、DB、Redis、向量库、各 service）。
P2 先提供身份依赖；P3 起补充 get_db / get_redis / get_vector_store 等。
"""

from typing import Optional

from fastapi import Header

from app.core.security import Principal, resolve_principal


def get_principal(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None),
) -> Principal:
    """FastAPI 依赖：从请求头解析调用方身份（详见 core.security.resolve_principal）。"""
    return resolve_principal(x_api_key, authorization)
