"""P2.4 验收 demo：口径守卫 + 空结果守卫。

三个场景：
  1. 命中口径（验收核心）：engine.ask("黑五 GMV") → 口径守卫报告无违规、SQL 含 2017-11+price+非取消
  2. 口径违规自愈：构造 GMV 缺「非取消」过滤的 SQL → 守卫检出违规 → 注入提示重写 → 复检
  3. 空结果自愈：构造 0 行 SQL（过滤值不存在）→ 空结果守卫重写 1 轮

运行：uv run python scripts/demo_guards.py
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chatbi.engine import ChatBIEngine  # noqa: E402
from chatbi.guards import check_caliber  # noqa: E402


def main() -> int:
    engine = ChatBIEngine()
    try:
        # 场景 1：黑五 GMV —— 应命中口径
        q1 = "2017 年黑五（11 月）的 GMV 是多少？"
        print("=" * 68)
        print(f"[场景1·命中口径] {q1}")
        ans = engine.ask(q1)
        print(f"  生成 SQL：{ans.sql}")
        print(f"  口径违规：{ans.caliber_violations or '无（命中口径 ✓）'}")
        print(f"  结果：{ans.result.rows if ans.result else None}")

        # 场景 2：口径违规自愈 —— GMV 缺非取消过滤
        q2 = "总 GMV 是多少？"
        bad2 = "SELECT SUM(price) AS gmv FROM olist_order_items"
        print("\n" + "=" * 68)
        print(f"[场景2·口径违规自愈] {q2}")
        print(f"  构造违规 SQL：{bad2}")
        print(f"  守卫检出：{check_caliber(q2, bad2)}")
        res2 = engine.executor.execute(bad2)
        out2, tr2 = engine.apply_guards(q2, res2)
        print(f"  重写后 SQL：{tr2.get('caliber_rewrite_sql')}")
        print(f"  复检违规：{tr2.get('caliber_violations_final')}")
        print(f"  重写后结果：{out2.rows if out2.ok else out2.error}")

        # 场景 3：空结果自愈 —— 过滤值不存在
        q3 = "有多少笔处于 paid 状态的订单？"
        bad3 = "SELECT order_id FROM olist_orders WHERE order_status = 'paid'"
        print("\n" + "=" * 68)
        print(f"[场景3·空结果自愈] {q3}")
        print(f"  构造 0 行 SQL：{bad3}")
        res3 = engine.executor.execute(bad3)
        print(f"  首次执行行数：{res3.row_count}（触发空结果守卫）")
        out3, tr3 = engine.apply_guards(q3, res3)
        print(f"  重写后 SQL：{tr3.get('empty_rewrite_sql')}")
        print(f"  重写后行数：{tr3.get('empty_rewrite_rows')}")
    finally:
        engine.close()

    print("\n" + "=" * 68)
    print("守卫留痕见 logs/self_correction.jsonl（trigger=guards）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
