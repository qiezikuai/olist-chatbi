"""P2.1 验收测试：只读 SQL 执行工具的四道防线。

纯函数用例（白名单/LIMIT）不连库；集成用例经 chatbi_ro 实测（含慢查询被掐断）。
运行：uv run pytest -q
"""
import time

import pytest

from chatbi.executor import ReadOnlyExecutor, classify, force_limit


@pytest.fixture(scope="module")
def ex():
    e = ReadOnlyExecutor.from_env(timeout_s=3, max_rows=100)
    yield e
    e.close()


# ---------- 防线 1：语句白名单（纯函数，无需 DB） ----------
@pytest.mark.parametrize("sql", [
    "DELETE FROM olist_orders",
    "UPDATE olist_orders SET order_status='x'",
    "DROP TABLE olist_orders",
    "INSERT INTO olist_orders (order_id) VALUES ('1')",
    "TRUNCATE TABLE olist_orders",
    "SELECT 1; DROP TABLE olist_orders",           # 多语句
    "/* 前置注释 */ DELETE FROM olist_orders",       # 注释绕过
    "SELECT * FROM olist_orders INTO OUTFILE '/tmp/x'",
    "SELECT LOAD_FILE('/etc/passwd')",
    "",
    "   ",
])
def test_whitelist_rejects(sql):
    ok, _ = classify(sql)
    assert not ok, f"应拒绝但放行了：{sql!r}"


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM olist_orders",
    "WITH t AS (SELECT 1 AS a) SELECT * FROM t",
    "select * from olist_orders where order_id = 'a;b'",   # 字符串内分号不应误判
    "SELECT '-- 不是注释' AS x FROM olist_orders",          # 字符串内 -- 不应误判
    "SELECT * FROM olist_orders LIMIT 5",
])
def test_whitelist_allows(sql):
    ok, reason = classify(sql)
    assert ok, f"应放行但拒绝了：{sql!r} → {reason}"


# ---------- 防线 2：强制 LIMIT（纯函数） ----------
def test_force_limit_injects_when_absent():
    sql, injected = force_limit("SELECT * FROM olist_orders", 50)
    assert injected and sql.endswith("LIMIT 50")


def test_force_limit_skips_when_present():
    sql, injected = force_limit("SELECT * FROM olist_orders LIMIT 5", 50)
    assert not injected and sql.upper().endswith("LIMIT 5")


def test_force_limit_ignores_limit_inside_string():
    # 字符串里的 'limit' 不算真 LIMIT，应注入
    sql, injected = force_limit("SELECT 'no limit here' AS x FROM olist_orders", 20)
    assert injected and sql.endswith("LIMIT 20")


# ---------- 防线 1+3+4：集成（经 chatbi_ro 实测） ----------
def test_execute_select_ok(ex):
    r = ex.execute("SELECT COUNT(*) AS c FROM olist_orders")
    assert r.ok and r.row_count == 1 and r.rows[0][0] == 99441


def test_execute_blocks_dml_before_db(ex):
    r = ex.execute("DELETE FROM olist_orders WHERE 1=0")
    assert not r.ok
    assert r.error.code == 'BLOCKED' and r.error.stage == 'whitelist'


def test_execute_forces_limit(ex):
    r = ex.execute("SELECT order_id FROM olist_orders", max_rows=10)
    assert r.ok and r.row_count == 10 and r.sql.rstrip().endswith("LIMIT 10")


def test_execute_timeout_kills_slow_query(ex):
    t0 = time.perf_counter()
    r = ex.execute("SELECT SLEEP(10)")
    dt = time.perf_counter() - t0
    assert not r.ok and r.error.code == 'TIMEOUT'
    assert dt < 6, f"慢查询未被及时掐断，耗时 {dt:.1f}s"


def test_execute_error_normalized(ex):
    r = ex.execute("SELECT * FROM no_such_table_xyz")
    assert not r.ok
    assert r.error.code in ('SEMANTIC', 'DB_ERROR') and r.error.errno


def test_execute_syntax_error_normalized(ex):
    r = ex.execute("SELECTT bad syntax here")  # 首关键字非 SELECT → 白名单先拦
    assert not r.ok and r.error.code == 'BLOCKED'
