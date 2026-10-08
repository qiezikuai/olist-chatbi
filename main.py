"""ChatBI 端到端编排入口（CLI）。

链路：自然语言问题 → 检索 → 生成 SQL → 只读执行 → 校验 →（自纠错/守卫）→ 总结结论。
本文件是编排状态图的可见入口：逐步打印每一阶段，代码可逐行讲。

运行：
  uv run python main.py                       # 跑 3 个内置 demo 问题（默认数据集 olist）
  uv run python main.py "总 GMV 是多少？" ...   # 跑自定义问题
  uv run python main.py --dataset sakila "..."  # 换数据集（见 chatbi/datasets.py）

前置：该数据集的向量库需已训练（见 README「复现步骤」/ scripts/train.py --dataset）。
凭据约束：LLM Key 与 DB 凭据由 engine/executor 自行从 .env 与数据集对应的
config/db_*.env 读取，绝不打印。
"""
from __future__ import annotations

import argparse
import sys

from chatbi.datasets import resolve
from chatbi.engine import ChatBIEngine

# 中文 Windows 控制台/管道下 stdout 用 GBK，✓ 等符号会触发 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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
        err = ans.error
        detail = f"{err.code} — {err.message}" if err else "生成失败（无错误详情）"
        print(f"  ① 检索+生成 ✗ 失败：{detail}")
        return False
    print(f"  ① 检索+生成 ✓ SQL：\n     {ans.sql}")

    # 执行 + 校验阶段
    if not ans.ok:
        e = ans.error
        if e is not None:
            print(f"  ② 执行 ✗ 失败：code={e.code} stage={e.stage} errno={e.errno}")
            print(f"     {e.message[:160]}")
        else:
            print("  ② 执行 ✗ 失败（无错误详情）")
        print("  ③ 校验：未通过（当前链路不做自动重试）")
        return False
    r = ans.result
    print(f"  ② 执行 ✓ 返回 {r.row_count} 行，耗时 {r.elapsed_ms}ms")
    print(f"  ③ 校验 ✓ 执行成功、结果结构完整")

    # 守卫状态（空结果 / 口径）
    notes = []
    if ans.empty_retried:
        notes.append("空结果守卫已改写 1 轮")
    if ans.caliber_violations:
        notes.append("口径仍未命中：" + "；".join(ans.caliber_violations))
    print(f"  ④ 守卫（空结果/口径）：{'；'.join(notes) if notes else '通过（口径命中 / 无空结果）'}")

    # 总结阶段
    print(f"  ⑤ 总结：\n{ans.summary}")
    return True


def dataset_demo_questions(ds, n: int = 3) -> list[str]:
    """非内置数据集的示例问题：取其问答对前 n 题（这些 SQL 在接入时已实跑验证过）。"""
    from chatbi.datasets import load_materials
    try:
        return [qa["question"] for qa in load_materials(ds).qa_pairs[:n]]
    except Exception:
        return []      # 材料缺失/损坏时不崩，由调用方给出明确提示


def main() -> int:
    ap = argparse.ArgumentParser(description="ChatBI 端到端问数（CLI）")
    ap.add_argument("--dataset", default="olist", help="数据集名，默认 olist")
    ap.add_argument("questions", nargs="*", help="要问的问题；不给则用该数据集的示例问题")
    args = ap.parse_args()

    try:
        ds = resolve(args.dataset)
    except RuntimeError as e:
        print(f"数据集解析失败：{e}")
        return 2

    questions = args.questions or (DEMO_QUESTIONS if ds.name == "olist"
                                   else dataset_demo_questions(ds))
    if not questions:
        print("未给出问题，且该数据集没有可用的示例问答对。请在命令行直接给出问题。")
        return 2

    print("ChatBI · 端到端编排（LangGraph 状态图）")
    if ds.name != "olist":
        print(f"数据集：{ds.name}（{ds.label}）")
    print(f"待答问题 {len(questions)} 个；执行链：检索→生成→执行(只读闸)→校验→总结")

    try:
        engine = ChatBIEngine(dataset=ds.name)
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
