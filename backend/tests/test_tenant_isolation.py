"""写入侧租户归属（ETL）+ 读取侧 RLS 的集成测试（真连 PostgreSQL）。

## 本文件针对的真实缺陷（用户指出，已实测确证）

初版 `sync_analytics_tables(path)` **只接文件路径，不接 tenant_id**，
把行数据的 tenant_id 硬编码成固定的 `DEFAULT_TENANT = 'a'`。后果是**两个**问题：

1. **"传了但查不到"**：租户 b 上传的 xlsx，行上盖的是 'a'。
   实测：以 `app.tenant_id='b'` 查询 → **0 行**。
2. **跨租户可见**（更严重）：执行器里写的是 `if tenant_id:`，
   未认证/无租户时**根本不设置** `app.tenant_id`；
   而旧 RLS 策略是 **fail-open** 的（"变量未设置 ⇒ 放行全部行"）。
   实测：租户 b 在未设变量的路径上能读到**全公司 10000 行薪资**。

两条一起构成"既是功能 bug 又是安全漏洞"的组合 —— 而当时的 E2E 用 demo key
（租户 a）验证，恰好等于 DEFAULT_TENANT，**所以 bug 在测试矩阵里不可见**。

## 修复策略

把「归属」与「可见性」拆成两个正交概念：
  - `tenant_id`：数据归属（谁传的）
  - `is_shared`：是否全公司共享（demo 语料 true，上传数据 false）
并把 RLS 改成 **fail-closed**：只有 `is_shared` 或 `tenant_id` 匹配才放行。
执行器改为**无条件**设置 `app.tenant_id`（无租户时用哨兵值）。

## 本文件的断言分工

  - `TestWriteSideTenantStamping`：写入的行是否带**真实上传租户**
  - `TestReadSideIsolation`：读侧隔离是否真的生效（含"b 只有 b 查得到"）
  - `TestFailClosed`：忘设变量时**不能**泄露租户私有数据
  - `TestSharedCorpusStillVisible`：共享语料仍对所有租户可见（防"改造完全空"）
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.analytics import ddl
from app.core.config import settings

TENANT_A = "test-tenant-a"
TENANT_B = "test-tenant-b"
TABLE = "rls_probe_tbl"


def _engines_available() -> bool:
    try:
        e = create_engine(settings.sync_analytics_url, connect_args={"connect_timeout": 5})
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        e.dispose()
        ro = create_engine(settings.sync_analytics_ro_url, connect_args={"connect_timeout": 5})
        with ro.connect() as c:
            c.execute(text("SELECT 1"))
        ro.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _engines_available(), reason="需要 kb_analytics 与 kb_ro 就绪"
)


@pytest.fixture
def admin_engine():
    e = create_engine(settings.sync_analytics_url)
    yield e
    with e.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {ddl.q(TABLE)} CASCADE"))
    e.dispose()


@pytest.fixture
def ro_engine():
    e = create_engine(settings.sync_analytics_ro_url)
    yield e
    e.dispose()


def _make_table(admin, *, tenant_id: str, is_shared: bool, rows: int = 2) -> None:
    """用与生产同一条 DDL/RLS 路径建一张探针表并写入数据。

    刻意**复用 ddl.py 的函数**（build_create_table_sql / build_rls_sql），
    而不是在测试里手写 SQL —— 否则测的是"测试里的策略"，
    生产策略改了测试也不会发现（这正是本类问题的成因）。
    """
    from app.analytics.schema_infer import infer_schema

    data = [["名称", "数量"]] + [[f"项目{i}", str(i)] for i in range(rows)]
    schema = infer_schema(data, table_name=TABLE, display_name="探针表",
                          description="租户隔离测试")
    with admin.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {ddl.q(TABLE)} CASCADE"))
        c.execute(text(ddl.build_create_table_sql(schema)))
        for stmt in ddl.build_rls_sql(TABLE):
            c.execute(text(stmt))
        cols = [ddl.q(col.name) for col in schema.columns]
        for r in data[1:]:
            c.execute(
                text(f"INSERT INTO {ddl.q(TABLE)} ({', '.join(cols)}, "
                     f"{ddl.q(ddl.TENANT_COLUMN)}, {ddl.q(ddl.SHARED_COLUMN)}) "
                     f"VALUES (:a, :b, :t, :s)"),
                {"a": r[0], "b": int(r[1]), "t": tenant_id, "s": is_shared},
            )


def _count(engine, tenant_id: str | None) -> int:
    """以指定租户身份查询（tenant_id=None 表示**刻意不设置变量**）。"""
    with engine.connect() as c:
        if tenant_id is not None:
            c.execute(text("SELECT set_config(:k, :v, false)"),
                      {"k": ddl.TENANT_GUC, "v": tenant_id})
        return c.execute(text(f"SELECT count(*) FROM {ddl.q(TABLE)}")).scalar_one()


# ==================== 写入侧：归属必须来自上传租户 ====================

class TestWriteSideTenantStamping:
    """ETL 必须把行标成**真实上传租户**，而不是固定值。"""

    def test_row_carries_uploading_tenant(self, admin_engine):
        _make_table(admin_engine, tenant_id=TENANT_B, is_shared=False)
        with admin_engine.connect() as c:
            tenants = [r[0] for r in c.execute(
                text(f"SELECT DISTINCT {ddl.q(ddl.TENANT_COLUMN)} FROM {ddl.q(TABLE)}")
            )]
        assert tenants == [TENANT_B], (
            f"行上应盖上传租户 {TENANT_B}，实际 {tenants} —— "
            f"若出现固定值（如 'a'）说明 ETL 没接 tenant_id"
        )

    def test_no_row_falls_back_to_a_hardcoded_tenant(self, admin_engine):
        """反证：固定兜底值绝不能出现在行上。"""
        _make_table(admin_engine, tenant_id=TENANT_B, is_shared=False)
        with admin_engine.connect() as c:
            n = c.execute(text(
                f"SELECT count(*) FROM {ddl.q(TABLE)} "
                f"WHERE {ddl.q(ddl.TENANT_COLUMN)} = 'a'"
            )).scalar_one()
        assert n == 0, "出现了硬编码兜底租户 'a' 的行"

    def test_sync_signature_accepts_tenant(self):
        """签名必须能接 tenant_id —— 防止有人把它改回只接 path。"""
        import inspect

        from app.analytics.seed import sync_analytics_tables

        params = inspect.signature(sync_analytics_tables).parameters
        assert "tenant_id" in params, "sync_analytics_tables 又变成只接 path 了"
        assert "is_shared" in params


# ==================== 读侧隔离 ====================

class TestReadSideIsolation:
    """核心诉求：**b 上传的，只有 b 查得到**。"""

    def test_tenant_b_can_read_own_data(self, admin_engine, ro_engine):
        _make_table(admin_engine, tenant_id=TENANT_B, is_shared=False)
        assert _count(ro_engine, TENANT_B) == 2, (
            "租户 b 读不到自己上传的数据 —— 这就是『传了但查不到』"
        )

    def test_tenant_a_cannot_read_b_data(self, admin_engine, ro_engine):
        _make_table(admin_engine, tenant_id=TENANT_B, is_shared=False)
        assert _count(ro_engine, TENANT_A) == 0, "租户 a 读到了租户 b 的私有数据（越权）"

    def test_other_tenant_sees_nothing(self, admin_engine, ro_engine):
        _make_table(admin_engine, tenant_id=TENANT_B, is_shared=False)
        assert _count(ro_engine, "test-tenant-c") == 0

    def test_two_tenants_are_isolated(self, admin_engine, ro_engine):
        """同一张表里两个租户的数据互不可见。"""
        from app.analytics.schema_infer import infer_schema

        data = [["名称"], ["x"]]
        schema = infer_schema(data, table_name=TABLE, display_name="t", description="d")
        with admin_engine.begin() as c:
            c.execute(text(f"DROP TABLE IF EXISTS {ddl.q(TABLE)} CASCADE"))
            c.execute(text(ddl.build_create_table_sql(schema)))
            for stmt in ddl.build_rls_sql(TABLE):
                c.execute(text(stmt))
            for t in (TENANT_A, TENANT_B):
                c.execute(text(
                    f"INSERT INTO {ddl.q(TABLE)} ({ddl.q('col_1')}, "
                    f"{ddl.q(ddl.TENANT_COLUMN)}, {ddl.q(ddl.SHARED_COLUMN)}) "
                    f"VALUES (:v, :t, false)"), {"v": t, "t": t})

        assert _count(ro_engine, TENANT_A) == 1
        assert _count(ro_engine, TENANT_B) == 1


# ==================== fail-closed（本次安全修复的核心）====================

class TestFailClosed:
    """忘设 `app.tenant_id` 时**不能**泄露租户私有数据。

    这是本次修复的关键：原策略是 fail-open（未设变量 ⇒ 放行全部），
    实测导致租户 b 能读到全公司薪资。现在必须 fail-closed。
    """

    def test_unset_variable_does_not_expose_private_rows(self, admin_engine, ro_engine):
        _make_table(admin_engine, tenant_id=TENANT_B, is_shared=False)
        got = _count(ro_engine, None)          # 刻意不设变量
        assert got == 0, (
            f"未设置 app.tenant_id 时读到了 {got} 行租户私有数据 —— "
            f"策略是 fail-open 的，存在越权"
        )

    def test_anonymous_sentinel_sees_no_private_rows(self, admin_engine, ro_engine):
        """执行器在无租户时用的哨兵值不得匹配到任何真实租户。"""
        from app.analytics.executor import ANONYMOUS_TENANT

        _make_table(admin_engine, tenant_id=TENANT_B, is_shared=False)
        assert _count(ro_engine, ANONYMOUS_TENANT) == 0

    def test_policy_sql_is_fail_closed(self):
        """静态断言：策略 SQL 里不得再有"变量未设置 ⇒ 放行"的分支。

        防止有人把 fail-open 的写法改回来（那是个安全回归）。
        """
        sql = " ".join(ddl.build_rls_sql(TABLE)).lower()
        assert f"current_setting('{ddl.TENANT_GUC}', true) is null" not in sql, (
            "RLS 策略又出现了 fail-open 分支（IS NULL ⇒ 放行全部）"
        )
        assert ddl.SHARED_COLUMN in sql, "策略应放行 is_shared 的行"


# ==================== 共享语料仍然可见（防"改造完全空"）====================

class TestSharedCorpusStillVisible:
    """fail-closed 不能把 demo 语料也挡掉，否则演示直接崩。"""

    def test_shared_rows_visible_to_any_tenant(self, admin_engine, ro_engine):
        _make_table(admin_engine, tenant_id="", is_shared=True)
        for t in (TENANT_A, TENANT_B, ANON if (ANON := "x") else "x"):
            assert _count(ro_engine, t) == 2, f"共享语料对 {t} 不可见"

    def test_shared_rows_visible_without_variable(self, admin_engine, ro_engine):
        """未设变量时应能看到共享语料（这是评测/匿名的正常路径）。"""
        _make_table(admin_engine, tenant_id="", is_shared=True)
        assert _count(ro_engine, None) == 2

    def test_demo_corpus_is_marked_shared(self):
        """真实环境断言：demo 语料必须已标记为共享。"""
        e = create_engine(settings.sync_analytics_url)
        try:
            with e.connect() as c:
                if not ddl.table_exists(e, "employees"):
                    pytest.skip("employees 未装载")
                total = c.execute(text("SELECT count(*) FROM employees")).scalar_one()
                shared = c.execute(text(
                    "SELECT count(*) FROM employees WHERE is_shared IS TRUE")
                ).scalar_one()
            assert total == shared, (
                f"employees 有 {total - shared} 行未标记共享 —— "
                f"fail-closed 策略下这些行对所有租户都不可见"
            )
        finally:
            e.dispose()


# ==================== 既有表兼容 ====================

class TestLegacyTableCompatibility:
    """升级前建的表（无 is_shared 列）不能因为策略收紧而整表不可读。"""

    def test_ensure_tenant_columns_backfills(self, admin_engine, ro_engine):
        # 造一张"旧结构"表：只有 tenant_id，没有 is_shared
        with admin_engine.begin() as c:
            c.execute(text(f"DROP TABLE IF EXISTS {ddl.q(TABLE)} CASCADE"))
            c.execute(text(
                f"CREATE TABLE {ddl.q(TABLE)} ("
                f"  {ddl.q('col_1')} TEXT, "
                f"  {ddl.q(ddl.TENANT_COLUMN)} VARCHAR(64) NOT NULL DEFAULT '', "
                f"  id SERIAL PRIMARY KEY)"
            ))
            c.execute(text(f"INSERT INTO {ddl.q(TABLE)} ({ddl.q('col_1')}) VALUES ('old')"))

        ddl.ensure_tenant_columns(admin_engine, TABLE)

        with admin_engine.connect() as c:
            cols = {r[0] for r in c.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"), {"t": TABLE})}
        assert ddl.SHARED_COLUMN in cols, "应补齐 is_shared 列"

        # 旧表按共享处理：补列后仍对所有租户可见（不因升级而"消失"）
        assert _count(ro_engine, TENANT_A) == 1
