"""租户模型。

`tenant_key` 是与 Qdrant payload 的 tenant_id、以及 .env 中 RAG_TENANT_KEYS 对应的字符串标识；
用字符串而非自增 FK 关联，是为了让「配置驱动的鉴权」与「PG 驱动的租户登记」平滑共存，
待 P8 落地 JWT/RBAC 后可再将其提升为真正的外键与权限表。
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Tenant(Base, TimestampMixin):
    """租户登记。"""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 与检索过滤字段值一致（如 "t1"）；唯一
    tenant_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tenant id={self.id} key={self.tenant_key!r} name={self.name!r}>"
