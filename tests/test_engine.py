"""P2.2 验收测试：编排引擎的「总结」步骤（纯函数，不调 LLM、不连库）。

端到端 3 问的验收证据见 PROJECT_BRIEF 进度日志（main.py 实跑输出）。
这里只覆盖 _summarize 的三种返回形态：单值 / 0 行 / 多行表格。
"""
from chatbi.engine import ChatBIEngine
from chatbi.executor import SqlResult


def test_summarize_single_value():
    r = SqlResult(ok=True, rows=[(98207,)], columns=["order_count"], row_count=1)
    s = ChatBIEngine._summarize("总共有多少笔订单？", r)
    assert "98207" in s and "总共有多少笔订单" in s


def test_summarize_zero_rows():
    r = SqlResult(ok=True, rows=[], columns=["x"], row_count=0)
    assert "0 行" in ChatBIEngine._summarize("任意问题", r)


def test_summarize_multi_row_table():
    r = SqlResult(ok=True, rows=[("a", 1), ("b", 2)], columns=["cat", "cnt"], row_count=2)
    s = ChatBIEngine._summarize("q", r)
    assert "| cat | cnt |" in s and "| a | 1 |" in s and "| b | 2 |" in s


def test_summarize_truncates_over_10_rows():
    rows = [(i,) for i in range(15)]
    r = SqlResult(ok=True, rows=rows, columns=["n"], row_count=15)
    s = ChatBIEngine._summarize("q", r)
    assert "共 15 行" in s and s.count("| ") <= 12  # 表头2行 + 数据10行
