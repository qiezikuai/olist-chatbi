"""前端优化验收脚本（优化指令 §6.2–§6.6、§6.8）——手动运行，产出证据：

    uv run python scripts/accept_ui.py

覆盖：黑五 GMV 结论含 1,003,862.14 / 订单数 98,207 / 多轮历史+1 且引擎单实例 /
chips 点击触发 / 图表或指标卡出现 / 口径守卫徽标经 run_sql 注入缺陷 SQL 点亮。
§6.1（headless health）与 §6.7（diff 范围）由执行官另行核验并记录。
"""
from pathlib import Path as _P
from streamlit.testing.v1 import AppTest

APP_PATH = _P(__file__).resolve().parent.parent / "app.py"

PASS = []


def check(name: str, cond: bool, evidence: str = ""):
    PASS.append((name, bool(cond), evidence))
    print(f"[{'OK' if cond else 'FAIL'}] {name}" + (f" ｜ {evidence}" if evidence else ""))


def all_markdown(at) -> str:
    return "\n".join(m.value for m in at.markdown)


def n_plotly(at) -> int:
    try:
        return len(at.get("plotly_chart"))
    except Exception:
        return 0


# ---------- 场景 1：黑五 GMV（结论数字 + SQL + 指标卡/图表） ----------
at = AppTest.from_file(str(APP_PATH), default_timeout=300)
at.run()
at.chat_input[0].set_value("2017 年黑五的 GMV 是多少？")
at.run()
md = all_markdown(at)
check("§6.2 结论含 1,003,862.14", "1,003,862.14" in md, md.splitlines()[-1][:60] if md.splitlines() else "")
check("§6.2 SQL 可见", len(at.code) >= 1 and "SUM" in at.code[0].value.upper(), at.code[0].value[:60] if at.code else "")
check("§6.2/6.6 指标卡或图表出现", len(at.metric) >= 1 or n_plotly(at) >= 1,
      f"metric={len(at.metric)}, plotly={n_plotly(at)}")
check("§6.6 会话历史 +1", len(at.session_state["history"]) == 1)
check("§6.2 徽标条常显只读", "chatbi_ro 只读" in md)

# ---------- 场景 2：追问第二轮（历史不丢 + 98,207） ----------
at.chat_input[0].set_value("总共有多少笔订单？")
at.run()
md2 = all_markdown(at)
check("§6.3 第二轮 98,207", "98,207" in md2)
check("§6.4 历史不丢（2 轮）", len(at.session_state["history"]) == 2)
check("§6.4 第一轮结论仍在页", "1,003,862.14" in md2)

# ---------- 场景 3：chips 点击即提问 ----------
at2 = AppTest.from_file(str(APP_PATH), default_timeout=300)
at2.run()
chip_idx = [i for i, b in enumerate(at2.button) if b.label == "总共有多少笔订单？"]
check("§6.5 chips 存在", len(chip_idx) == 1, f"buttons={[b.label for b in at2.button][:5]}")
at2.button[chip_idx[0]].click()
at2.run()
check("§6.5 chip 点击触发提问", len(at2.session_state["history"]) == 1 and "98,207" in all_markdown(at2))

# ---------- 场景 4：口径守卫徽标（run_sql 注入缺陷 SQL，复用 demo_guards 构造） ----------
from chatbi.engine import ChatBIEngine  # noqa: E402

eng = ChatBIEngine()
bad_sql = "SELECT SUM(price) AS gmv FROM olist_order_items"  # 缺「非取消」过滤
ans = eng.run_sql("总 GMV 是多少？", bad_sql)
check("§6.8 守卫确实干预（guard_trace）", bool(ans.guard_trace.get("caliber_trigger")),
      f"violations={ans.guard_trace.get('caliber_violations')}")
at3 = AppTest.from_file(str(APP_PATH), default_timeout=300)
at3.session_state["history"] = [("总 GMV 是多少？", ans)]
at3.run()
md3 = all_markdown(at3)
check("§6.8 徽标「口径守卫命中」亮出", "口径守卫命中" in md3)
check("§6.8 时间线含 guards 环", "guards" in md3)
check("§6.8 修正后数值=非取消 GMV 锚点", "13,494,400.74" in md3)

# ---------- 场景 5：多行两列类目题 → 横向条形图（plotly 渲染） ----------
at4 = AppTest.from_file(str(APP_PATH), default_timeout=300)
at4.run()
at4.chat_input[0].set_value("最热门的 5 个商品类目是什么？")
at4.run()
check("§6.6 多行类目题渲染 plotly 条形图", n_plotly(at4) >= 1, f"plotly={n_plotly(at4)}")
check("§6 条形图题表格同时可见", len(at4.dataframe) >= 1)

fails = [n for n, ok, _ in PASS if not ok]
print()
print(f"== 验收 {'全部通过' if not fails else '存在失败: ' + str(fails)}（{len(PASS) - len(fails)}/{len(PASS)}）==")
raise SystemExit(1 if fails else 0)
