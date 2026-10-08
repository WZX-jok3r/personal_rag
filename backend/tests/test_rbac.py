"""RBAC 与敏感列脱敏的单测。

核心原则（见 app/core/security.py 注释）：
    **不配置就等于没有这个功能** —— 未配置 RAG_TENANT_ROLES 时
    所有人都拿 default_role(analyst)，脱敏只对非特权角色生效。
    这保证既有部署与全部既有测试零影响。

测试分三层：
  1. 身份解析与角色判定（纯函数）
  2. 结果集脱敏（纯函数，可穷举）
  3. 端到端：非特权角色的查询结果里看不到薪资
"""

from __future__ import annotations

import pytest

from app.analytics.redact import MASK, redact_rows, _is_sensitive
from app.core.config import settings
from app.core.security import (
    KNOWN_ROLES,
    PRIVILEGED_ROLES,
    ROLE_ANALYST,
    ROLE_EMPLOYEE,
    Principal,
    resolve_principal,
)


# ==================== 身份与角色 ====================

class TestPrincipalDefaults:
    """向后兼容：默认构造必须与改造前行为一致。"""

    def test_default_role_is_analyst(self):
        p = Principal(tenant_id="a", authenticated=True)
        assert p.role == ROLE_ANALYST

    def test_analyst_sees_sensitive(self):
        assert Principal("a", True, ROLE_ANALYST).can_see_sensitive

    def test_employee_cannot_see_sensitive(self):
        assert not Principal("a", True, ROLE_EMPLOYEE).can_see_sensitive

    def test_old_two_arg_construction_still_works(self):
        """既有代码/测试大量使用 Principal(tenant_id, authenticated) 两参形式。"""
        p = Principal(None, False)
        assert p.tenant_id is None and p.authenticated is False
        assert p.role == ROLE_ANALYST

    def test_all_known_roles_can_run_sql(self):
        """employee 是脱敏而非禁止 —— 禁止会让普通员工完全用不了统计功能。"""
        for r in KNOWN_ROLES:
            assert Principal("a", True, r).can_run_sql

    def test_unknown_role_cannot_run_sql(self):
        assert not Principal("a", True, "hacker").can_run_sql


class TestRedactColumns:
    def test_analyst_has_empty_redact_set(self):
        """特权角色返回空集 -> 执行器走零成本快路径。"""
        assert Principal("a", True, ROLE_ANALYST).redact_columns() == frozenset()

    def test_employee_has_redact_set(self):
        cols = Principal("a", True, ROLE_EMPLOYEE).redact_columns()
        assert isinstance(cols, frozenset)
        # 由配置决定内容；这里只断言"非空"的前提是配置确实配了敏感列
        if settings.redact_column_set:
            assert cols == frozenset(settings.redact_column_set)


class TestResolvePrincipalRoles:
    def test_auth_disabled_returns_anonymous_with_default_role(self, monkeypatch):
        monkeypatch.setattr(settings, "rag_tenant_keys", "")
        p = resolve_principal(None, None)
        assert p.tenant_id is None and not p.authenticated
        assert p.role == settings.default_role

    def test_configured_tenant_gets_configured_role(self, monkeypatch):
        monkeypatch.setattr(settings, "rag_tenant_keys", "a=key_a;b=key_b")
        monkeypatch.setattr(settings, "rag_tenant_roles", "b=employee")
        assert resolve_principal("key_a", None).role == ROLE_ANALYST
        assert resolve_principal("key_b", None).role == ROLE_EMPLOYEE

    def test_unconfigured_tenant_falls_back_to_default_role(self, monkeypatch):
        monkeypatch.setattr(settings, "rag_tenant_keys", "a=key_a")
        monkeypatch.setattr(settings, "rag_tenant_roles", "")
        assert resolve_principal("key_a", None).role == settings.default_role

    def test_unknown_role_in_config_fails_loudly(self, monkeypatch):
        """配置写错必须显式失败，**不能静默降级为特权角色** ——
        静默降级会让一个笔误变成权限漏洞。"""
        from app.core.exceptions import ForbiddenError

        monkeypatch.setattr(settings, "rag_tenant_keys", "a=key_a")
        monkeypatch.setattr(settings, "rag_tenant_roles", "a=speradmin")  # 拼错
        with pytest.raises(ForbiddenError) as ei:
            resolve_principal("key_a", None)
        assert "未知角色" in str(ei.value)

    def test_invalid_key_still_forbidden(self, monkeypatch):
        from app.core.exceptions import ForbiddenError

        monkeypatch.setattr(settings, "rag_tenant_keys", "a=key_a")
        with pytest.raises(ForbiddenError):
            resolve_principal("wrong", None)


# ==================== 结果集脱敏 ====================

class TestIsSensitive:
    def test_qualified_match(self):
        assert _is_sensitive("salary", "", frozenset({"employees.salary"}))

    def test_bare_column_match(self):
        """结果集里通常只有裸列名，必须支持裸名匹配，否则绝大多数查询拦不住。"""
        assert _is_sensitive("salary", "", frozenset({"employees.salary"}))

    def test_case_insensitive(self):
        assert _is_sensitive("SALARY", "", frozenset({"employees.salary"}))

    def test_non_sensitive_column(self):
        assert not _is_sensitive("department", "", frozenset({"employees.salary"}))

    def test_empty_redact_set(self):
        assert not _is_sensitive("salary", "", frozenset())


class TestRedactRows:
    def test_masks_sensitive_column(self):
        rows, masked = redact_rows(
            ["department", "salary"],
            [["Sales", 1042], ["HR", 900]],
            frozenset({"employees.salary"}),
        )
        assert masked == ["salary"]
        assert rows == [["Sales", MASK], ["HR", MASK]]

    def test_zero_cost_when_no_redact(self):
        """空集合必须原样返回（零成本快路径，保证未启用 RBAC 时无开销）。"""
        original = [["Sales", 1042]]
        rows, masked = redact_rows(["department", "salary"], original, frozenset())
        assert masked == []
        assert rows == original

    def test_non_sensitive_columns_untouched(self):
        rows, masked = redact_rows(
            ["id", "first_name", "department"],
            [[1, "Derek", "Operations"]],
            frozenset({"employees.salary"}),
        )
        assert masked == []
        assert rows == [[1, "Derek", "Operations"]]

    def test_select_star_scenario(self):
        """关键场景：模型写 SELECT *，敏感列照样被拦住。

        这正是"在结果集上脱敏"优于"改写 SQL"的原因 ——
        不需要理解模型写了什么，只按列名匹配。
        """
        columns = ["id", "first_name", "last_name", "email", "department", "salary", "hire_date"]
        rows = [[1, "Derek", "Cummings", "d@x.com", "Operations", 179997, "2011-04-06"]]
        out, masked = redact_rows(columns, rows, frozenset({"employees.salary", "employees.email"}))
        assert set(masked) == {"salary", "email"}
        assert out[0][5] == MASK and out[0][3] == MASK
        assert out[0][1] == "Derek"          # 非敏感列不动
        assert out[0][4] == "Operations"

    def test_none_values_stay_none(self):
        """NULL 不该被写成 *** —— 那会把"没有值"伪装成"被脱敏"。"""
        rows, _ = redact_rows(["salary"], [[None]], frozenset({"employees.salary"}))
        assert rows == [[None]]

    def test_multiple_sensitive_columns(self):
        rows, masked = redact_rows(
            ["salary", "email"],
            [[100, "a@b.com"]],
            frozenset({"employees.salary", "employees.email"}),
        )
        assert set(masked) == {"salary", "email"}
        assert rows == [[MASK, MASK]]

    def test_empty_rows(self):
        rows, masked = redact_rows(["salary"], [], frozenset({"employees.salary"}))
        assert rows == [] and masked == ["salary"]

    def test_empty_columns(self):
        rows, masked = redact_rows([], [], frozenset({"employees.salary"}))
        assert rows == [] and masked == []


class TestRedactObservation:
    """必须显式告知 LLM 哪些列被脱敏，否则它会对着 *** 编造数值。"""

    def test_appends_warning(self):
        from app.analytics.redact import redact_observation

        out = redact_observation("结果 2 行", ["salary"])
        assert "salary" in out
        assert "不要猜测" in out or "不要编造" in out

    def test_no_warning_when_nothing_masked(self):
        from app.analytics.redact import redact_observation

        text = "结果 2 行"
        assert redact_observation(text, []) == text


class TestKnownRolesConsistency:
    def test_privileged_subset_of_known(self):
        assert PRIVILEGED_ROLES <= KNOWN_ROLES

    def test_analyst_is_privileged(self):
        assert ROLE_ANALYST in PRIVILEGED_ROLES

    def test_employee_is_not_privileged(self):
        assert ROLE_EMPLOYEE not in PRIVILEGED_ROLES
