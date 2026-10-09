"""鉴权单测（P2 门禁）：resolve_principal 各分支，行为对齐 src/auth 语义。

与框架解耦，直接调用 resolve_principal；通过 monkeypatch settings.rag_tenant_keys
（tenant_keys / auth_enabled 均由其派生）来切换鉴权开关与租户映射。
"""

import pytest

from app.core import security as sec
from app.core.exceptions import ForbiddenError, UnauthorizedError


@pytest.fixture
def two_tenants(monkeypatch):
    monkeypatch.setattr(sec.settings, "rag_tenant_keys", "t1=key1;t2=key2")


def test_auth_disabled_returns_anonymous(monkeypatch):
    monkeypatch.setattr(sec.settings, "rag_tenant_keys", "")
    p = sec.resolve_principal(None, None)
    assert p.tenant_id is None
    assert p.authenticated is False


def test_valid_x_api_key(two_tenants):
    p = sec.resolve_principal("key1", None)
    assert p.tenant_id == "t1"
    assert p.authenticated is True


def test_bearer_token_fallback(two_tenants):
    p = sec.resolve_principal(None, "Bearer key2")
    assert p.tenant_id == "t2"
    assert p.authenticated is True


def test_missing_key_raises_401(two_tenants):
    with pytest.raises(UnauthorizedError):
        sec.resolve_principal(None, None)


def test_invalid_key_raises_403(two_tenants):
    with pytest.raises(ForbiddenError):
        sec.resolve_principal("nope", None)
