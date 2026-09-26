"""ui_helpers 纯函数单测：格式化 / 图表推断 / 徽标 / 时间线 / CSV。"""
import datetime as dt
from types import SimpleNamespace

from chatbi.ui_helpers import (
    badge_items,
    build_figure,
    col_label,
    fmt_display_text,
    fmt_value,
    infer_chart_kind,
    is_money_col,
    timeline_steps,
    to_csv,
)


def _answer(**kw):
    base = dict(ok=True, question="q", sql="SELECT 1", summary="s", stage="ok",
                result=None, error=None, self_healed=False, trace={},
                caliber_violations=[], empty_retried=False, guard_trace={})
    base.update(kw)
    return SimpleNamespace(**base)


def _result(rows, columns, **kw):
    base = dict(ok=True, sql="", rows=rows, columns=columns, row_count=len(rows),
                elapsed_ms=12, error=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_col_label_and_money():
    assert col_label("total_gmv") == "GMV"
    assert col_label("weird_col") == "weird_col"
    assert is_money_col("total_gmv") and is_money_col("payment_value")
    assert not is_money_col("order_count")


def test_fmt_value():
    assert fmt_value(1003862.14, money=True) == "¥1,003,862.14"
    assert fmt_value(98207) == "98,207"
    assert fmt_value(4.2) == "4.20"
    assert fmt_value(None) == "-"


def test_fmt_display_text_skips_years():
    assert fmt_display_text("→ 98207") == "→ 98,207"
    assert fmt_display_text("1003862.14") == "1,003,862.14"
    assert fmt_display_text("2017 年黑五") == "2017 年黑五"          # 年份不加逗号
    assert fmt_display_text("2018-01 月 7187 单") == "2018-01 月 7,187 单"


def test_infer_chart_kind():
    assert infer_chart_kind(["order_count"], [(98207,)]) == "metric"
    assert infer_chart_kind(["a", "b"], [(1, 2)]) == "metrics"
    assert infer_chart_kind(["month", "gmv"], [("2017-11", 1.0), ("2017-12", 2.0)]) == "line"
    assert infer_chart_kind(["state", "order_count"], [("SP", 1), ("RJ", 2)]) == "bar"
    assert infer_chart_kind(["a", "b", "c"], [(1, 2, 3), (4, 5, 6)]) == "table"
    assert infer_chart_kind(["a"], []) == "empty"


def test_infer_date_like_by_values():
    rows = [(dt.date(2017, 11, 1), 1), (dt.date(2017, 12, 1), 2)]
    assert infer_chart_kind(["d", "v"], rows) == "line"


def test_build_figure_returns_none_for_non_chart_kinds():
    assert build_figure("metric", None) is None
    assert build_figure("table", None) is None


def test_badge_items_success_and_conditions():
    a = _answer(result=_result([(1,)], ["order_count"]))
    texts = [t for t, _ in badge_items(a)]
    assert texts == ["1 行 · 耗时 12 ms · chatbi_ro 只读"]

    a2 = _answer(result=_result([(1,)], ["x"]), self_healed=True,
                 guard_trace={"caliber_trigger": True, "caliber_violations": ["GMV 未排除取消单"]})
    texts2 = [t for t, _ in badge_items(a2)]
    assert "自纠错 1 轮" in texts2 and "口径守卫命中" in texts2

    a3 = _answer(result=_result([(1,)], ["x"]), caliber_violations=["仍违规"])
    assert "口径仍违规" in [t for t, _ in badge_items(a3)]

    a4 = _answer(ok=False, stage="execute", error=SimpleNamespace(message="boom"))
    assert "失败 · 停在 execute 阶段" in [t for t, _ in badge_items(a4)]


def test_timeline_steps_guard_and_heal():
    a = _answer(self_healed=True, trace={"attempt0_error": "unknown column"},
                guard_trace={"empty_trigger": True})
    names = [n for n, _ in timeline_steps(a)]
    assert names == ["generate", "execute", "self_correct", "guards", "summarize"]


def test_to_csv():
    out = to_csv([(1, "SP")], ["order_count", "state"])
    assert out.strip().splitlines() == ["order_count,state", "1,SP"]
