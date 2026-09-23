"""P2.4 验收测试：口径守卫 + 空结果判定（纯函数，不调 LLM、不连库）。

端到端「命中口径 / 违规重写 / 空结果重写」证据见 scripts/demo_guards.py 实跑输出。
"""
from chatbi.executor import SqlResult
from chatbi.guards import check_caliber, is_empty_result

GOOD_BF_GMV = (
    "SELECT SUM(oi.price) FROM olist_order_items oi "
    "JOIN olist_orders o ON oi.order_id = o.order_id "
    "WHERE o.order_status NOT IN ('canceled','unavailable') "
    "AND o.order_purchase_timestamp >= '2017-11-01' AND o.order_purchase_timestamp < '2017-12-01'"
)


def test_black_friday_gmv_hits_caliber():
    # 验收核心：黑五 GMV 类问题命中口径（GMV 口径 + 黑五口径都满足）→ 无违规
    assert check_caliber("2017 年黑五（11 月）的 GMV 是多少？", GOOD_BF_GMV) == []


def test_gmv_missing_noncanceled_violates():
    bad = "SELECT SUM(price) FROM olist_order_items"
    v = check_caliber("总 GMV 是多少", bad)
    assert v and any("canceled" in x for x in v)


def test_black_friday_wrong_month_violates():
    bad = ("SELECT SUM(oi.price) FROM olist_order_items oi JOIN olist_orders o ON oi.order_id=o.order_id "
           "WHERE o.order_status NOT IN ('canceled','unavailable') AND o.order_purchase_timestamp >= '2017-10-01'")
    v = check_caliber("黑五的 GMV", bad)
    assert any("2017-11" in x for x in v)


def test_repurchase_must_use_unique_id():
    assert check_caliber("复购率是多少", "SELECT COUNT(DISTINCT customer_id) FROM olist_customers")
    assert check_caliber("有多少个客户", "SELECT COUNT(DISTINCT customer_unique_id) FROM olist_customers") == []


def test_category_must_use_translation():
    assert check_caliber("最热门的类目", "SELECT product_category_name FROM olist_products")
    good = ("SELECT t.product_category_name_english FROM olist_order_items oi "
            "JOIN olist_products p ON oi.product_id=p.product_id "
            "JOIN olist_product_category_translation t ON p.product_category_name=t.product_category_name")
    assert check_caliber("最热门的类目", good) == []


def test_no_intent_no_check():
    # 不涉及任何已固化口径的问题 → 不检查、不报违规
    assert check_caliber("一共有多少笔订单", "SELECT COUNT(*) FROM olist_orders") == []


def test_is_empty_result():
    assert is_empty_result(SqlResult(ok=True, rows=[], columns=["x"], row_count=0))
    assert not is_empty_result(SqlResult(ok=True, rows=[(1,)], columns=["x"], row_count=1))
    assert not is_empty_result(SqlResult(ok=False))   # 执行失败不算"空结果"


def test_payment_question_does_not_trigger_gmv_rule():
    # P3.4 回归：Q12 教训——"信用卡支付的总金额"是支付题，不应命中 GMV 口径规则被强行改写成 SUM(price)
    pay_sql = ("SELECT ROUND(SUM(pay.payment_value), 2) FROM olist_order_payments pay "
               "JOIN olist_orders o ON pay.order_id = o.order_id "
               "WHERE o.order_status NOT IN ('canceled','unavailable') AND pay.payment_type = 'credit_card'")
    assert check_caliber("信用卡支付的总金额是多少？", pay_sql) == []
    # 真·GMV 问题仍应正常校验
    assert check_caliber("总 GMV 是多少", "SELECT SUM(price) FROM olist_order_items")  # 缺非取消过滤→违规
