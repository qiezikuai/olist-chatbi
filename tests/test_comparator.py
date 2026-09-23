"""P3.2 验收测试：结果比对器（验收标准要求比对器自身通过 ≥3 个单测）。

全部纯函数，不连库、不调 LLM。
"""
import datetime
from decimal import Decimal

from chatbi.comparator import normalize_cell, normalize_result, results_match


def test_row_order_agnostic():
    ref = [("SP", 100), ("RJ", 50), ("MG", 30)]
    gen = [("MG", 30), ("SP", 100), ("RJ", 50)]   # 同集合、换序
    ok, _ = results_match(ref, gen)
    assert ok


def test_numeric_tolerance_and_types():
    # int/float/Decimal/数字字符串 归一后相等；2 位容差内视为一致
    assert results_match([(Decimal("100.00"),)], [(100,)])[0]
    assert results_match([(137.424,)], [(137.42,)])[0]      # 四舍五入到 2 位后相等
    assert results_match([("100",)], [(100.0,)])[0]        # 数字字符串 vs 数值
    assert not results_match([(137.42,)], [(137.43,)])[0]  # 超容差 → 不一致


def test_real_mismatch():
    ok, reason = results_match([("SP", 100)], [("RJ", 100)])
    assert not ok and "不一致" in reason


def test_row_count_differs():
    ok, reason = results_match([("a", 1), ("b", 2)], [("a", 1)])
    assert not ok and "行数不同" in reason


def test_column_count_differs():
    ok, reason = results_match([("a", 1)], [("a",)])
    assert not ok and "列数不同" in reason


def test_string_case_and_whitespace():
    assert results_match([(" SP ",)], [("sp",)])[0]
    assert normalize_cell("  Health_Beauty ") == "health_beauty"


def test_datetime_and_none():
    d = datetime.date(2018, 1, 1)
    assert results_match([(d,)], [(datetime.datetime(2018, 1, 1),)])[0]  # date vs datetime ISO 一致
    assert results_match([(None,)], [(None,)])[0]
    assert normalize_cell(None) is None


def test_known_conservative_representation_diff():
    # 文档化的保守取舍：月份 '2018-01'(串) vs 1(整) 语义同但表示异 → 判不一致（准确率被低估而非高估）
    ok, _ = results_match([(1, 7187)], [("2018-01", 7187)])
    assert not ok


def test_normalize_result_is_sorted_and_stable():
    rows = [("b", 2), ("a", 1)]
    n = normalize_result(rows)
    assert n == sorted(n) and len(n) == 2
