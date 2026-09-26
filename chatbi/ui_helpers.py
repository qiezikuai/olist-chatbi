"""前端纯函数层：列名映射 / 数字格式化 / 图表推断 / 徽标 / 编排时间线 / CSV。

只消费 Answer/SqlResult 既有字段（契约见 PROJECT_BRIEF 与优化指令 §3），不碰引擎四层。
全部为纯函数（build_figure 除外，仅依赖传入的 DataFrame），便于单测。
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import re

# ---------- 列名 → 中文标签 ----------
COL_LABELS = {
    "total_gmv": "GMV",
    "gmv": "GMV",
    "order_count": "订单数",
    "unique_customers": "客户数",
    "rep_rate": "复购率(%)",
    "rep_customers": "复购客户数",
    "aov": "客单价",
    "avg_score": "平均评分",
    "avg_delivery_days": "平均配送天数",
    "customer_state": "州",
    "state": "州",
    "city": "城市",
    "product_category_name_english": "类目",
    "category": "类目",
    "month": "月份",
    "year": "年份",
    "price": "金额",
    "payment_value": "支付金额",
    "freight_value": "运费",
    "review_score": "评分",
    "seller_id": "卖家",
}

MONEY_HINTS = ("gmv", "price", "amount", "revenue", "aov", "value", "freight")
DATE_NAME_HINTS = ("date", "month", "year", "week", "day", "time", "_dt")

BRAND_BLUE = "#1F3864"


def col_label(name: str) -> str:
    return COL_LABELS.get(str(name).lower(), str(name))


def is_money_col(name: str) -> bool:
    n = str(name).lower()
    return any(h in n for h in MONEY_HINTS)


def fmt_value(v, money: bool = False) -> str:
    """单值格式化：金额 ¥+千分位2位；浮点2位；整数千分位；其余原样。"""
    if v is None:
        return "-"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int,)):
        return f"¥{v:,.2f}" if money else f"{v:,}"
    if isinstance(v, float):
        return f"¥{v:,.2f}" if money else f"{v:,.2f}"
    return str(v)


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _comma(m: re.Match) -> str:
    tok = m.group(0)
    if "," in tok:
        return tok
    int_part, _, dec = tok.partition(".")
    n = int(int_part)
    if not dec:
        if len(int_part) == 4 and 1900 <= n <= 2100:  # 年份不动
            return tok
        if len(int_part) >= 4:
            return f"{n:,}"
        return tok
    if len(int_part) >= 4:
        return f"{n:,}.{dec}"
    return tok


def fmt_display_text(text: str) -> str:
    """展示层千分位：给结论文本里的大数字加逗号（4 位年份除外），不改语义。"""
    return _NUM_RE.sub(_comma, text or "")


# ---------- 图表推断 ----------
def _first_col_date_like(columns: list, rows: list) -> bool:
    if not rows:
        return False
    name = str(columns[0]).lower()
    if any(h in name for h in DATE_NAME_HINTS):
        return True
    vals = [r[0] for r in rows[:20]]
    if all(isinstance(v, (_dt.date, _dt.datetime)) for v in vals):
        return True
    if all(isinstance(v, str) for v in vals):
        import warnings

        import pandas as pd

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # 日期格式推断告警为展示层噪音
            parsed = pd.to_datetime(pd.Series(vals), errors="coerce")
        return bool(parsed.notna().mean() >= 0.8)
    return False


def infer_chart_kind(columns: list, rows: list) -> str:
    """按优化指令 §5 的形状规则选图型：metric/metrics/line/bar/table/empty。"""
    if not rows:
        return "empty"
    n, c = len(rows), len(columns)
    if n == 1 and c == 1:
        return "metric"
    if n == 1 and c > 1:
        return "metrics"
    if n > 1 and c == 2:
        return "line" if _first_col_date_like(columns, rows) else "bar"
    return "table"


def build_figure(kind: str, df):
    """line/bar → plotly Figure；其余返回 None（调用方降级为表格/指标卡）。"""
    if kind not in ("line", "bar") or df is None or df.empty or df.shape[1] < 2:
        return None
    import plotly.express as px

    c0, c1 = df.columns[0], df.columns[1]
    labels = {c0: col_label(c0), c1: col_label(c1)}
    if kind == "line":
        fig = px.line(df, x=c0, y=c1, labels=labels, markers=True)
        fig.update_traces(line=dict(color=BRAND_BLUE, width=2.5), marker=dict(color=BRAND_BLUE))
    else:
        ordered = df.iloc[::-1]  # 首行(最大)显示在最上
        fig = px.bar(ordered, x=c1, y=c0, orientation="h", labels=labels)
        fig.update_traces(marker_color=BRAND_BLUE)
    fig.update_layout(
        template="plotly_white",
        margin=dict(l=20, r=20, t=30, b=20),
        font=dict(size=13),
    )
    if is_money_col(c1):
        axis = "y" if kind == "line" else "x"
        fig.update_layout({f"{axis}axis_tickprefix": "¥"})
    return fig


# ---------- 徽标条 ----------
def badge_items(answer) -> list[tuple[str, str]]:
    """(文案, 色调) 列表。常显元信息 + 仅触发时亮出的条件徽标。

    口径守卫语义（见 DECISIONS 前端优化条）：guard_trace.caliber_trigger=干预发生过；
    answer.caliber_violations 非空=最终仍违规（更严重的告警）。
    """
    items: list[tuple[str, str]] = []
    res = getattr(answer, "result", None)
    if answer.ok and res is not None and res.ok:
        items.append((f"{res.row_count} 行 · 耗时 {res.elapsed_ms} ms · chatbi_ro 只读", "info"))
    else:
        items.append((f"失败 · 停在 {answer.stage or '未知'} 阶段", "bad"))
    if getattr(answer, "self_healed", False):
        items.append(("自纠错 1 轮", "warn"))
    gt = getattr(answer, "guard_trace", None) or {}
    if gt.get("caliber_trigger"):
        items.append(("口径守卫命中", "warn"))
    if getattr(answer, "caliber_violations", None):
        items.append(("口径仍违规", "bad"))
    if gt.get("empty_trigger") or getattr(answer, "empty_retried", False):
        items.append(("空结果改写 1 轮", "warn"))
    return items


# ---------- 编排时间线 ----------
def timeline_steps(answer) -> list[tuple[str, str]]:
    steps = [("generate", "检索（DDL/口径/历史SQL）+ LLM 生成 SQL")]
    res = getattr(answer, "result", None)
    if res is not None and res.ok:
        steps.append(("execute", f"只读执行闸通过：{res.row_count} 行 / {res.elapsed_ms} ms"))
    elif res is not None:
        steps.append(("execute", f"执行闸拦截/失败：{res.error.message if res.error else '未知'}"))
    else:
        steps.append(("execute", f"未到达执行（stage={answer.stage}）"))
    if getattr(answer, "self_healed", False):
        t = getattr(answer, "trace", None) or {}
        detail = "报错回喂 LLM 重写 1 轮后通过"
        if t.get("attempt0_error"):
            detail = f"首轮报错 {str(t['attempt0_error'])[:60]} → 重写后通过"
        steps.append(("self_correct", detail))
    gt = getattr(answer, "guard_trace", None) or {}
    if gt:
        parts = []
        if gt.get("empty_trigger"):
            parts.append("空结果改写")
        if gt.get("caliber_trigger"):
            parts.append("口径违规重写：" + "；".join(gt.get("caliber_violations", [])))
        steps.append(("guards", " / ".join(parts) or "守卫检查"))
    steps.append(("summarize", "确定性总结（不再调 LLM）"))
    return steps


# ---------- CSV 导出 ----------
def to_csv(rows: list, columns: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    w.writerows(rows)
    return buf.getvalue()
