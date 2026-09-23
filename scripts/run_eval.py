"""P3.3：评估跑分 + 报告产出 → eval/report.md（方向标要求的 30 题准确率数字）。

流程：对 eval/questions.yaml 每题——
  1. 走完整引擎 engine.ask(question)（检索→生成→执行→自纠错→守卫→总结）得到「生成结果」
  2. 用只读执行器跑「标准 SQL」得到「参考结果」
  3. chatbi.comparator.results_match 行级规范化比对判分
准确率口径（PLAN_v2 第 7 节）：分母=30，分子=执行成功且结果一致；SQL 文本相似度不参与判定。

运行：uv run python scripts/run_eval.py
红线：Key/DB 凭据由 engine/executor 自读，绝不打印、不入报告。
"""
from __future__ import annotations

import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chatbi.engine import ChatBIEngine          # noqa: E402
from chatbi.comparator import results_match     # noqa: E402

TYPES = ["单表聚合", "多表关联", "时序环比", "排名对比"]


def load_questions() -> list[dict]:
    data = yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text(encoding="utf-8"))
    return data["questions"]


def attribute(rec: dict) -> str:
    """把失败归因为：生成失败 / 执行失败 / 口径不符 / 结果不一致 / 比对器保守(表示差异)。"""
    if rec["status"] == "生成失败":
        return "生成失败（未产出可用 SQL）"
    if rec["status"] == "执行失败":
        return f"执行失败（{rec.get('err_code')}）"
    if rec["status"] == "口径不符":
        return "口径不符（守卫修正后仍未命中）"
    reason = rec.get("reason", "")
    if "列数不同" in reason or "行数不同" in reason:
        return "结果结构不同（行/列数）"
    return "结果不一致（值或表示差异，含比对器保守判定）"


def main() -> int:
    questions = load_questions()
    engine = ChatBIEngine()
    records: list[dict] = []
    print(f"===== P3.3 评估跑分：{len(questions)} 题 =====")
    try:
        for i, q in enumerate(questions, 1):
            qid, qtype, question, std_sql = q["id"], q["type"], q["question"], q["sql"]
            rec = {"id": qid, "type": qtype, "question": question, "std_sql": std_sql}
            try:
                # 参考结果（标准 SQL 经同一只读执行器，规范化路径一致）
                ref = engine.executor.execute(std_sql)
                ref_rows = ref.rows if ref.ok else None

                # 生成结果（完整引擎链路）
                ans = engine.ask(question)
                rec["gen_sql"] = ans.sql
                rec["self_healed"] = ans.self_healed
                rec["caliber_fixed"] = bool(ans.guard_trace.get("caliber_trigger"))
                rec["empty_retried"] = bool(ans.guard_trace.get("empty_trigger"))

                if not ans.ok or ans.result is None or not ans.result.ok:
                    rec.update(status="生成失败" if ans.stage == "generate" else "执行失败",
                               match=False, reason=ans.error.message if ans.error else "无结果",
                               err_code=ans.error.code if ans.error else None)
                elif ref_rows is None:
                    rec.update(status="参考SQL失败", match=False, reason="标准 SQL 执行失败（评估集需修正）")
                elif ans.caliber_violations:
                    # 命中口径守卫仍违规 → 单列"口径不符"
                    m, reason = results_match(ref_rows, ans.result.rows)
                    rec.update(status="口径不符", match=m, reason="；".join(ans.caliber_violations),
                               gen_rows=ans.result.rows[:6])
                else:
                    m, reason = results_match(ref_rows, ans.result.rows)
                    rec.update(status="通过" if m else "结果不一致", match=m, reason=reason,
                               ref_rows=ref_rows[:6], gen_rows=ans.result.rows[:6])
            except Exception as e:
                rec.update(status="异常", match=False, reason=f"{type(e).__name__}: {str(e)[:140]}")
            records.append(rec)
            flag = "✓" if rec.get("match") else "✗"
            extra = " [自愈]" if rec.get("self_healed") else ""
            extra += " [口径修正]" if rec.get("caliber_fixed") else ""
            print(f"[{i:>2}/30 {qid}|{qtype}] {flag} {rec.get('status')}{extra} | {question}")
            if not rec.get("match"):
                print(f"        原因：{rec.get('reason','')[:120]}")
                if rec.get("gen_sql"):
                    print(f"        genSQL：{rec['gen_sql'][:160]}")
    finally:
        engine.close()

    write_report(records)
    return 0


def write_report(records: list[dict]) -> None:
    total = len(records)
    passed = sum(1 for r in records if r.get("match"))
    by_type = {t: [0, 0] for t in TYPES}
    for r in records:
        if r["type"] in by_type:
            by_type[r["type"]][1] += 1
            if r.get("match"):
                by_type[r["type"]][0] += 1
    healed = sum(1 for r in records if r.get("self_healed"))
    caliber_fixed = sum(1 for r in records if r.get("caliber_fixed"))
    empty_retried = sum(1 for r in records if r.get("empty_retried"))
    bad = [r for r in records if not r.get("match")]

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    L = []
    L.append("# ChatBI 评估报告")
    L.append("")
    L.append(f"> 生成时间：{ts}｜运行：`uv run python scripts/run_eval.py`")
    L.append("> 评估集：`eval/questions.yaml`（30 题留出集，与训练 QA 不逐字重合）")
    L.append("> 引擎：vanna RAG（三路训练）+ DeepSeek-V3.2 + 自纠错 1 轮 + 口径/空结果守卫")
    L.append("> 准确率口径（PLAN_v2 第 7 节）：分母=30，分子=执行成功且**结果一致**（行级规范化比对，SQL 文本不计）")
    L.append("")
    L.append("## 总准确率")
    L.append("")
    L.append(f"**{passed}/{total} = {passed/total*100:.1f}%**")
    L.append("")
    L.append("## 分类别准确率")
    L.append("")
    L.append("| 题型 | 准确率 |")
    L.append("|---|---|")
    for t in TYPES:
        p, n = by_type[t]
        L.append(f"| {t} | {p}/{n} = {p/n*100:.0f}% |")
    L.append("")
    L.append("## 自纠错 / 守卫贡献")
    L.append("")
    L.append(f"- 经自纠错（P2.3）后成功：{healed} 题")
    L.append(f"- 经口径守卫（P2.4）触发修正：{caliber_fixed} 题")
    L.append(f"- 经空结果守卫（P2.4）触发改写：{empty_retried} 题")
    L.append("")
    L.append("## Bad case 归因")
    L.append("")
    if not bad:
        L.append("无失败题。")
    else:
        L.append("| id | 题型 | 状态 | 归因 |")
        L.append("|---|---|---|---|")
        for r in bad:
            L.append(f"| {r['id']} | {r['type']} | {r.get('status')} | {attribute(r)} |")
        L.append("")
        L.append("### 明细")
        for r in bad:
            L.append(f"- **{r['id']}（{r['question']}）**：{r.get('reason','')[:200]}")
            if r.get("gen_sql"):
                L.append(f"  - 生成 SQL：`{r['gen_sql'][:200]}`")
    L.append("")
    L.append("## 复现")
    L.append("")
    L.append("```bash")
    L.append("uv run python scripts/train_and_test.py   # 或 p1_5_sampling.py：训练向量库")
    L.append("uv run python scripts/run_eval.py          # 跑分并重生成本报告")
    L.append("```")
    L.append("")
    L.append("> 注：LLM 生成非确定性，复跑准确率可能小幅波动；本报告为单次运行结果。")
    L.append("> 比对器对「表示级差异」（如月份 '2018-01' vs 1）保守判不一致，只会低估准确率（见 comparator.py）。")

    out = ROOT / "eval" / "report.md"
    out.write_text("\n".join(L), encoding="utf-8")

    print("\n" + "=" * 56)
    print(f"P3.3 总准确率：{passed}/{total} = {passed/total*100:.1f}%")
    for t in TYPES:
        p, n = by_type[t]
        print(f"  {t}: {p}/{n} = {p/n*100:.0f}%")
    print(f"自愈 {healed} / 口径修正 {caliber_fixed} / 空结果改写 {empty_retried}")
    print(f"报告已写入：{out}")
    print("=" * 56)


if __name__ == "__main__":
    raise SystemExit(main())
