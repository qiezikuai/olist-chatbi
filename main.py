"""P2.2：ChatBI 端到端编排入口。

链路：自然语言问题 → 检索 → 生成 SQL → 只读执行 → 校验 → 总结结论。
本文件是"自写编排"的可见入口：逐步打印每一阶段，代码可逐行讲。

运行：
  uv run python main.py                       # 跑 3 个内置 demo 问题
  uv run python main.py "总 GMV 是多少？" ...   # 跑自定义问题

前置：向量库需已训练（见 README / scripts/train_and_test.py）。
红线：LLM Key 与 DB 凭据由 engine/executor 自行从 .env / config/db_ro.env 读取，绝不打印。
"""
from __future__ import annotations

import sys

from chatbi.engine import ChatBIEngine

# 3 个 demo 问题：覆盖「单值聚合 / 时序锚点 / 多行排名」三种返回形态
DEMO_QUESTIONS = [
    "总共有多少笔订单？",
    "2017 年黑五（11 月）的 GMV 是多少？",
    "最热门的 5 个商品类目是什么？",
]


def run_question(engine: ChatBIEngine, idx: int, question: str) -> bool:
    print("\n" + "=" * 64)
    print(f"[问题 {idx}] {question}")
    print("=" * 64)

    ans = engine.ask(question)   # 一次调用走完 检索→生成→执行→校验→总结

    # 生成阶段
    if ans.stage == "generate":
        print(f"  ① 检索+生成 ✗ 失败：{ans.error.code} — {ans.error.message}")
        return False
    print(f"  ① 检索+生成 ✓ SQL：\n     {ans.sql}")

    # 执行 + 校验阶段
    if not ans.ok:
        e = ans.error
        print(f"  ② 执行 ✗ 失败：code={e.code} stage={e.stage} errno={e.errno}")
        print(f"     {e.message[:160]}")
        print("  ③ 校验：未通过（P2.3 将在此按错误类别自纠错重写 1 轮）")
        return False
    r = ans.result
    print(f"  ② 执行 ✓ 返回 {r.row_count} 行，耗时 {r.elapsed_ms}ms")
    print(f"  ③ 校验 ✓ 执行成功、结果结构完整")

    # P2.4 守卫状态
    notes = []
    if ans.empty_retried:
        notes.append("空结果守卫已改写 1 轮")
    if ans.caliber_violations:
        notes.append("口径仍未命中：" + "；".join(ans.caliber_violations))
    print(f"  ④ 守卫(P2.4)：{'；'.join(notes) if notes else '通过（口径命中 / 无空结果）'}")

    # 总结阶段
    print(f"  ⑤ 总结：\n{ans.summary}")
    return True


def main() -> int:
    questions = sys.argv[1:] or DEMO_QUESTIONS
    print("ChatBI · 自写编排主循环（P2.2）")
    print(f"待答问题 {len(questions)} 个；执行链：检索→生成→执行(只读闸)→校验→总结")

    try:
        engine = ChatBIEngine()
    except RuntimeError as e:
        print(f"初始化失败：{e}")
        return 2

    ok_count = 0
    try:
        for i, q in enumerate(questions, 1):
            if run_question(engine, i, q):
                ok_count += 1
    finally:
        engine.close()

    print("\n" + "=" * 64)
    print(f"端到端结果：{ok_count}/{len(questions)} 个问题成功走通全链路")
    print("=" * 64)
    return 0 if ok_count == len(questions) else 1


if __name__ == "__main__":
    raise SystemExit(main())
