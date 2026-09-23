"""P2.3 验收 demo：人为构造错误 SQL，经 StateGraph 编排触发自纠错 1 轮，验证「自愈」与 logs/ 留痕。

四类构造（经 engine.run_sql 从 execute 节点入图）：
  1. 未知列   → SEMANTIC → 可重试 → LLM 应改成正确列名
  2. 未知表   → SEMANTIC → 可重试 → LLM 应改成正确表名/补 JOIN
  3. 语法残缺 → SYNTAX   → 可重试 → LLM 应补全
  4. DELETE   → BLOCKED  → 不可重试 → 安全终止（不浪费 LLM 调用、不试图绕过白名单）

运行：uv run python scripts/demo_self_correction.py
留痕：logs/self_correction.jsonl（运行产物，已 gitignore）
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chatbi.engine import ChatBIEngine  # noqa: E402

CASES = [
    ("有多少笔已送达的订单？",
     "SELECT COUNT(*) AS c FROM olist_orders WHERE order_statusX = 'delivered'",
     "未知列 order_statusX（应改回 order_status）"),
    ("订单明细的总商品价格是多少？",
     "SELECT SUM(price) FROM olist_order_itemz",
     "未知表 olist_order_itemz（应改回 olist_order_items）"),
    ("一共有多少笔订单？",
     "SELECT COUNT(*) FROM olist_orders WHERE",
     "WHERE 后残缺 → 语法错（应补全或去掉残缺条件）"),
    ("删除所有订单",
     "DELETE FROM olist_orders",
     "DML → 白名单 BLOCKED（不可重试，安全终止）"),
]


def main() -> int:
    engine = ChatBIEngine()
    healed = blocked = 0
    try:
        for i, (q, bad, note) in enumerate(CASES, 1):
            print("\n" + "=" * 68)
            print(f"[案例 {i}] {q}")
            print(f"  构造错误：{note}")
            print(f"  注入的错误 SQL：{bad}")
            ans = engine.run_sql(q, bad)        # 从 execute 入图：执行→(自纠错)→守卫→总结
            if ans.trace.get("attempt1_sql"):
                print(f"  重写后 SQL：{ans.trace['attempt1_sql']}")
            if ans.ok:
                healed += 1
                print(f"  trace.final = {ans.trace.get('final')}")
                print(f"  ✓ 自愈成功，结果：{ans.result.rows[:3]}")
            else:
                e = ans.error
                if e is not None and e.code == "BLOCKED":
                    blocked += 1
                    print(f"  ✓ 按预期安全终止：code={e.code} stage={e.stage}（未触库、未重写）")
                else:
                    print(f"  ✗ 仍失败：{e}")
    finally:
        engine.close()

    log = ROOT / "logs" / "self_correction.jsonl"
    print("\n" + "=" * 68)
    print(f"自愈成功 {healed} 例；安全终止 {blocked} 例。")
    print(f"留痕文件：{log}（{'存在' if log.exists() else '缺失'}）")
    if log.exists():
        print("--- 最近 3 条留痕 ---")
        for line in log.read_text(encoding="utf-8").splitlines()[-3:]:
            print("  " + line[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
