"""
security.py - API 鉴权与多租户身份解析

设计（"API Key 每租户一密钥 + 单 collection + tenant_id 字段"）:
- 请求头带 X-API-Key（或 Authorization: Bearer <key>）来标识调用方。
- 服务端把 Key 映射到一个租户（tenant_id），后续检索按该租户强制过滤。
- 软关闭：未配置任何租户 Key（settings.tenant_keys 为空）时，鉴权不启用，
  返回 tenant_id=None 的匿名主体，检索不做租户过滤——行为与加鉴权前完全一致，
  不打断已有数据与本地调试。

扩展预留:
- 若要升级为 JWT/RBAC，只需替换 resolve_principal 的实现，保持返回 Principal
  （含 tenant_id 及可选的 roles/scopes）即可，下游过滤逻辑无需改动。
"""

from dataclasses import dataclass
from typing import Optional

from app.core.config import settings
from app.core.exceptions import ForbiddenError, UnauthorizedError


@dataclass
class Principal:
    """已解析的调用方身份"""
    tenant_id: Optional[str]      # 租户标识；None 表示匿名（鉴权关闭时）
    authenticated: bool           # 是否已通过 API Key 认证


def resolve_principal(x_api_key: Optional[str], authorization: Optional[str]) -> Principal:
    """
    解析并校验调用方身份（与框架无关，便于单测直接调用）。
    - 鉴权关闭：直接放行，返回匿名主体（tenant_id=None）。
    - 鉴权开启：缺失 Key -> 401；Key 无效 -> 403；有效 -> 返回对应租户主体。
    """
    if not settings.auth_enabled:
        return Principal(tenant_id=None, authenticated=False)

    # 优先取 X-API-Key，其次 Bearer token
    key = x_api_key
    if not key and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            key = token.strip()

    if not key:
        raise UnauthorizedError("缺少 API Key（X-API-Key 或 Authorization: Bearer）")

    tenant_id = settings.tenant_keys.get(key)
    if tenant_id is None:
        raise ForbiddenError("API Key 无效或无权限")

    return Principal(tenant_id=tenant_id, authenticated=True)
