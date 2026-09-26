"""ChatBI 网页前端（Streamlit 壳）v2 —— 答案优先 + 多轮对话 + 自动图表 + 品牌视觉。

纯壳：只调用现成的 ChatBIEngine.ask()，不碰 executor/guards/comparator/graph 四层；
图表数据只取 Answer.result（rows/columns/row_count）。
凭据红线：Key 走 .env、DB 走 config/db_ro.env，均由引擎内部读取，本文件不接触任何凭据。

启动：uv run streamlit run app.py
"""
from pathlib import Path

import pandas as pd
import streamlit as st

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

CSS_PATH = Path(__file__).resolve().parent / "assets" / "style.css"

st.set_page_config(page_title="小数点 · 电商问数助手", page_icon="🪙", layout="centered")
st.markdown(f"<style>{CSS_PATH.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


@st.cache_resource(show_spinner="加载问数引擎（MySQL 只读连接 + 向量库冷加载）…")
def get_engine():
    """缓存引擎实例：多轮追问不重连 MySQL、不重载 chroma。"""
    from chatbi.engine import ChatBIEngine

    return ChatBIEngine()


engine = get_engine()

# ---------- 侧边栏：数据源 / 安全模型 / 问法示例 ----------
with st.sidebar:
    st.markdown("### 📦 数据源")
    st.markdown(
        "Olist 电商数仓：**9 表 / 1,550,922 行**（2016-09 ~ 2018-10）。\n\n"
        "查询仅经 `chatbi_ro` **只读账号**（仅 SELECT 权限）。"
    )
    st.markdown("### 🛡 安全模型")
    st.markdown(
        "双保险：①应用层**只读执行闸**（语句白名单 + 强制 LIMIT + 服务端超时）；"
        "②数据库只读账号兜底。LLM 生成的 SQL 不可信，权限层是硬闸。"
    )
    st.markdown("### 💡 问法示例")
    st.markdown(
        "- 总共有多少笔订单？\n"
        "- 2017 年黑五的 GMV 是多少？\n"
        "- 哪个州的订单量最多？\n"
        "- 最热门的 5 个商品类目是什么？\n"
        "- 复购率是多少？"
    )

# ---------- 标题区 ----------
st.title("🪙 小数点 · 电商问数助手")
st.caption(
    "你说中文，我来查数——自动写 SQL、查 155 万行电商数仓、把答案端上来。"
    "试试：总共有多少笔订单？／2017 年黑五的 GMV 是多少？／哪个州卖得最好？"
)

# ---------- 示例 chips ----------
CHIPS = ["总共有多少笔订单？", "2017 年黑五的 GMV 是多少？", "TOP5 类目", "哪个州卖得最好？"]
_chip_cols = st.columns(len(CHIPS))
for _col, _label in zip(_chip_cols, CHIPS):
    if _col.button(_label, key=f"chip_{_label}"):
        st.session_state["pending"] = _label

if "history" not in st.session_state:
    st.session_state.history = []  # list[(question, Answer)]


def _render_badges(answer) -> None:
    spans = " ".join(
        f'<span class="dc-badge dc-badge--{tone}">{text}</span>'
        for text, tone in badge_items(answer)
    )
    st.markdown(spans, unsafe_allow_html=True)


def _render_answer(answer, turn_idx: int) -> None:
    _render_badges(answer)

    if not answer.ok:
        err_msg = answer.error.message if answer.error else "未知错误"
        st.error(f"问数失败（停在 {answer.stage or '未知'} 阶段）：{err_msg}")

    with st.container(border=True):  # 答案卡片：结论 → 图表 → 表格
        if answer.summary:
            text = fmt_display_text(answer.summary)
            if "\n" not in text.strip() and "|" not in text:
                st.markdown(f'<div class="dc-big">{text}</div>', unsafe_allow_html=True)
            else:
                st.markdown(text)

        res = answer.result
        if res is not None and res.ok and res.rows:
            df = pd.DataFrame(res.rows, columns=list(res.columns))
            kind = infer_chart_kind(list(res.columns), res.rows)
            if kind == "metric":
                name = res.columns[0]
                st.metric(col_label(name), fmt_value(res.rows[0][0], is_money_col(name)))
            elif kind == "metrics":
                mcols = st.columns(len(res.columns))
                for mc, name, val in zip(mcols, res.columns, res.rows[0]):
                    mc.metric(col_label(name), fmt_value(val, is_money_col(name)))
            elif kind in ("line", "bar"):
                fig = build_figure(kind, df)
                if fig is not None:
                    st.plotly_chart(fig, width="stretch")
                st.dataframe(df, width="stretch", hide_index=True)
            else:
                st.dataframe(df, width="stretch", hide_index=True)
            st.download_button(
                "⬇ 导出 CSV",
                to_csv(res.rows, list(res.columns)),
                file_name=f"chatbi_result_{turn_idx}.csv",
                mime="text/csv",
                key=f"csv_{turn_idx}",
            )
        elif res is not None and res.ok:
            st.info("查询成功，但结果为 0 行。")

    if answer.sql:
        with st.expander("🧾 SQL（可复制）"):
            st.code(answer.sql, language="sql")

    with st.expander("🔍 编排时间线（LangGraph 状态流转）"):
        for step_name, detail in timeline_steps(answer):
            st.markdown(f"- **{step_name}** — {detail}")
        if answer.trace:
            st.markdown("**自纠错留痕**")
            st.json(answer.trace, expanded=False)
        if answer.guard_trace:
            st.markdown("**守卫留痕**")
            st.json(answer.guard_trace, expanded=False)


def _render_turn(question: str, answer, turn_idx: int) -> None:
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        _render_answer(answer, turn_idx)


# ---------- 历史会话渲染（刷新不丢：session_state 随会话保留） ----------
for _i, (_q, _a) in enumerate(st.session_state.history):
    _render_turn(_q, _a, _i)

# ---------- 新问题：chips pending 或 chat_input ----------
_pending = st.session_state.pop("pending", None)
_user_q = st.chat_input("你想问什么？") or _pending
if _user_q:
    _user_q = str(_user_q).strip()
    if _user_q:
        with st.spinner("检索 → 生成 SQL → 只读执行 → 校验 → 总结…"):
            _answer = engine.ask(_user_q)
        st.session_state.history.append((_user_q, _answer))
        _render_turn(_user_q, _answer, len(st.session_state.history) - 1)
