"""P3.1：评估集人工验证——经 chatbi_ro 只读执行 30 条标准 SQL，核对锚点，产出验证快照。

做三件事：
  1. 读 eval/questions.yaml，逐条执行标准 SQL（只读账号，绝不打印凭据）。
  2. 对单值题核对锚点数字（与 docs/metrics.md 已验证锚点一致），对多行题核对行数。
  3. 把规范化后的结果快照写入 eval/verified_results.json（人工验证的机器可读证据，
     供 P3.3 检测 DB 漂移；P3.2 比对器仍以「实时执行标准 SQL」为准，见 PLAN 第 7 节）。

运行：uv run python scripts/verify_eval.py
退出码非 0 = 有标准 SQL 执行失败或锚点不符（需修正评估集）。
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pymysql
import yaml

ROOT = Path(__file__).resolve().parent.parent

# 单值题锚点（来自 docs/metrics.md 已验证数字 + P1.5 self-check 实测）
ANCHORS: dict[str, float] = {
    "Q01": 96478, "Q02": 96096, "Q03": 4.09, "Q04": 99224, "Q05": 32951,
    "Q06": 3095, "Q07": 625, "Q08": 120.65, "Q09": 13494400.74, "Q10": 137.42,
    "Q11": 5163867.22, "Q12": 12350042.56, "Q15": 12698, "Q16": 19539,
    "Q17": 75707, "Q19": 1003862.14, "Q20": 660179.62, "Q21": 7423,
    "Q22": 6108492.27, "Q24": 53532,
}
# 多行题期望行数
ROWCOUNTS: dict[str, int] = {
    "Q13": 5, "Q14": 4, "Q18": 5, "Q23": 9, "Q25": 5,
    "Q26": 5, "Q27": 5, "Q28": 5, "Q29": 5, "Q30": 5,
}


def connect() -> pymysql.connections.Connection:
    cfg = {}
    for line in (ROOT / "config" / "db_ro.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return pymysql.connect(host=cfg["host"], user=cfg["user"], password=cfg["password"],
                           database=cfg["database"], port=int(cfg["port"]),
                           read_timeout=60, connect_timeout=15)


def to_jsonable(rows) -> list:
    out = []
    for row in rows:
        cells = []
        for c in row:
            if isinstance(c, Decimal):
                cells.append(float(c))
            elif isinstance(c, (bytes, bytearray)):
                cells.append(c.decode("utf-8", "replace"))
            else:
                cells.append(c)
        out.append(cells)
    return out


def main() -> int:
    data = yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text(encoding="utf-8"))
    questions = data["questions"]
    print(f"评估集题量：{len(questions)}（meta.total={data['meta']['total']}）")
    assert len(questions) == data["meta"]["total"] == 30, "题量必须为 30"

    conn = connect()
    snapshot: dict[str, dict] = {}
    failures: list[str] = []
    print("\n===== 逐条执行标准 SQL（chatbi_ro 只读）=====")
    for q in questions:
        qid, qtype, sql = q["id"], q["type"], q["sql"]
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        except Exception as e:
            failures.append(f"{qid}: SQL ERROR {type(e).__name__} {str(e)[:120]}")
            print(f"[{qid}|{qtype}] ✗ 执行失败：{type(e).__name__} {str(e)[:120]}")
            continue

        js = to_jsonable(rows)
        snapshot[qid] = {"type": qtype, "row_count": len(rows), "result": js}

        # 锚点 / 行数核对
        note = ""
        if qid in ANCHORS:
            got = round(float(rows[0][0]), 2) if rows and rows[0][0] is not None else None
            exp = ANCHORS[qid]
            if got is None or abs(got - exp) > 0.01:
                failures.append(f"{qid}: 锚点不符 期望{exp} 实得{got}")
                note = f" ✗锚点不符(期望{exp} 实得{got})"
            else:
                note = f" ✓锚点={got}"
        elif qid in ROWCOUNTS:
            if len(rows) != ROWCOUNTS[qid]:
                failures.append(f"{qid}: 行数不符 期望{ROWCOUNTS[qid]} 实得{len(rows)}")
                note = f" ✗行数不符(期望{ROWCOUNTS[qid]} 实得{len(rows)})"
            else:
                note = f" ✓{len(rows)}行"
        preview = js[:3]
        print(f"[{qid}|{qtype}] OK rows={len(rows)}{note} | {preview}")
    conn.close()

    out = ROOT / "eval" / "verified_results.json"
    out.write_text(json.dumps(
        {"verified_by": "chatbi_ro", "total": len(snapshot), "results": snapshot},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n验证快照已写入：{out}")

    print("\n===== 验证小结 =====")
    if failures:
        print(f"✗ {len(failures)} 项需修正：")
        for f in failures:
            print("  -", f)
        return 1
    print(f"✅ 30/30 标准 SQL 全部执行成功；{len(ANCHORS)} 项锚点吻合、{len(ROWCOUNTS)} 项行数符合预期。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
