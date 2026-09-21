"""P2.3 验收测试：自纠错的错误分流与 SQL 清洗（纯函数，不调 LLM、不连库）。

端到端「自愈」证据见 scripts/demo_self_correction.py 实跑输出与 logs/ 留痕。
"""
from chatbi.engine import _clean_sql, is_retriable_error


def test_retriable_codes():
    for c in ["SEMANTIC", "SYNTAX", "TIMEOUT", "DB_ERROR"]:
        assert is_retriable_error(c), f"{c} 应可重试"


def test_non_retriable_codes():
    # BLOCKED/PERMISSION 是安全/权限拒绝，重写=试图绕过，必须终止
    for c in ["BLOCKED", "PERMISSION", "GENERATE_FAIL", "EMPTY_SQL", "WHATEVER"]:
        assert not is_retriable_error(c), f"{c} 不应重试"


def test_clean_sql_strips_markdown_fence():
    raw = "```sql\nSELECT COUNT(*) FROM olist_orders;\n```"
    assert _clean_sql(raw) == "SELECT COUNT(*) FROM olist_orders"


def test_clean_sql_extracts_select_from_prose():
    raw = "修正后的查询如下：\nSELECT SUM(price) FROM olist_order_items;  hope it helps"
    assert _clean_sql(raw) == "SELECT SUM(price) FROM olist_order_items"


def test_clean_sql_keeps_with_cte():
    raw = "WITH t AS (SELECT 1 a) SELECT * FROM t"
    assert _clean_sql(raw) == "WITH t AS (SELECT 1 a) SELECT * FROM t"
