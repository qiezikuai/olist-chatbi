"""ChatBI 网页前端（Streamlit 壳）——只调用现成的 ChatBIEngine.ask()，不碰底层四层。

启动：uv run streamlit run app.py
凭据红线：Key 走 .env、DB 走 config/db_ro.env，均由引擎内部读取，本文件不接触任何凭据。
"""
import pandas as pd
import streamlit as st

st.set_page_config(page_title="ChatBI · 电商问数 Agent", page_icon="📊", layout="centered")


@st.cache_resource(show_spinner="加载问数引擎（MySQL 只读连接 + 向量库冷加载）…")
def get_engine():
    """缓存引擎实例：避免每次提问重连 MySQL、重载 chroma（D5 冷加载约数秒）。"""
    from chatbi.engine import ChatBIEngine

    return ChatBIEngine()


st.title("🪙 小数点 · 电商问数助手")
st.caption(
    "你说中文，我来查数——自动写 SQL、查 155 万行电商数仓、把答案端上来。"
    "试试：总共有多少笔订单？／2017 年黑五的 GMV 是多少？／哪个州卖得最好？"
)

engine = get_engine()

question = st.text_input("你想问什么？", placeholder="比如：2017 年黑五的 GMV 是多少？")
if st.button("问一下", type="primary"):
    if question.strip():
        with st.spinner("检索 → 生成 SQL → 只读执行 → 校验 → 总结…"):
            st.session_state.answer = engine.ask(question.strip())
    else:
        st.session_state.answer = None
        st.warning("请先输入一个问题。")

answer = st.session_state.get("answer")
if answer is not None:
    st.divider()

    if not answer.ok:
        err_msg = answer.error.message if answer.error else "未知错误"
        st.error(f"问数失败（停在 {answer.stage or '未知'} 阶段）：{err_msg}")

    st.subheader("① 生成的 SQL")
    if answer.sql:
        st.code(answer.sql, language="sql")
    else:
        st.info("未生成 SQL。")

    st.subheader("② 执行结果")
    res = answer.result
    if res is not None and res.ok:
        if res.rows:
            st.caption(f"{res.row_count} 行 · 执行耗时 {res.elapsed_ms} ms（只读账号 chatbi_ro）")
            st.dataframe(
                pd.DataFrame(res.rows, columns=res.columns),
                width="stretch",
                hide_index=True,
            )
        else:
            st.info("查询成功，但结果为 0 行。")
    elif res is not None:
        st.warning(f"执行未通过：{res.error.message if res.error else '未知错误'}")

    st.subheader("③ 结论")
    if answer.summary:
        st.markdown(answer.summary)
    else:
        st.info("无结论输出。")

    # ---- 加分项：LangGraph 编排过程折叠展示 ----
    with st.expander("🔍 编排过程（LangGraph 状态流转）"):
        steps = ["generate（检索+生成 SQL）", "execute（只读执行闸）"]
        if answer.self_healed:
            steps.append("self_correct（报错回喂重写 1 轮）")
        if answer.empty_retried or answer.caliber_violations or answer.guard_trace:
            steps.append("guards（空结果/口径守卫）")
        steps.append("summarize（确定性总结）")
        st.markdown(" → ".join(f"`{s}`" for s in steps))

        meta = {
            "最终状态": "成功" if answer.ok else f"失败（{answer.stage}）",
            "自纠错触发": "是（1 轮）" if answer.self_healed else "否",
            "空结果改写": "是" if answer.empty_retried else "否",
            "口径守卫最终违规": answer.caliber_violations or "无（命中口径）",
        }
        st.json(meta, expanded=False)

        if answer.trace:
            st.markdown("**自纠错留痕**")
            st.json(answer.trace, expanded=False)
        if answer.guard_trace:
            st.markdown("**守卫留痕**")
            st.json(answer.guard_trace, expanded=False)
