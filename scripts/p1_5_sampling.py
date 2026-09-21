"""P1.5：30 题临时抽测 + 调优，验证 RAG 泛化能力（执行结果比对口径）

定位：这是 MVP 阶段的「临时抽测」，用于在搭编排层(P2)与正式评估集(P3.1)之前，
快速得到一个准确率信号并暴露 bad case。正式 30 题评估集(eval/questions.yaml)
+ pytest 比对器在 P3.1/P3.2 落地，本脚本不替代它们。

准确率口径（沿用 PLAN_v2 第 7 节）：
  分母 = 30；分子 = 生成 SQL 执行成功且结果与参考 SQL 一致的题数。
  结果集做「行级规范化」后比对：排序无关 + 数值容差(保留 2 位小数)。
  SQL 文本相似度不参与判定。

调优杠杆（口径注入 / 示例选择）：
  - 训练材料三路：9 DDL(schema.md) + 指标口径文档(metrics 摘要) + 10 组问答对(few-shot)。
  - 三路材料全部复用 train_and_test.py，口径单源、不重复维护。
  - chroma 竞态坑(D5)处置：rm -rf 干净重建 → 训练 → sleep(2) → 重新实例化后再查询。

运行：
  uv run python scripts/p1_5_sampling.py --selfcheck   # 仅执行 30 条参考 SQL，校验 ground truth
  uv run python scripts/p1_5_sampling.py               # 完整抽测（会调用 LLM，产生少量 token 费用）

红线：API Key / DB 密码由脚本自行从 .env / config/db_ro.env 读取，绝不打印、绝不入 git。
"""
from __future__ import annotations

import shutil
import sys
import time
from decimal import Decimal
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 复用 P1.4 的三路训练材料，保证口径单源
from train_and_test import QA_PAIRS, METRICS_CONTEXT, load_ddl  # noqa: E402

NC = "order_status NOT IN ('canceled','unavailable')"  # 默认非取消口径

# ------------------------------------------------------------------
# 30 题临时抽测集
# 题型分布(PLAN 第7节)：单表聚合 8 / 多表关联 10 / 时序环比 6 / 排名对比 6
# 每题 = {id, type, question, sql(参考/ground-truth)}
# 与 10 组训练问答对刻意不完全重叠，用于检验泛化而非记忆。
# ------------------------------------------------------------------
QUESTIONS: list[dict] = [
    # ---- 单表聚合 (8) ----
    {"id": "S1", "type": "单表聚合", "question": "一共有多少笔已送达的订单？",
     "sql": f"SELECT COUNT(*) FROM olist_orders WHERE order_status = 'delivered'"},
    {"id": "S2", "type": "单表聚合", "question": "平台上有多少个不同的自然人客户？",
     "sql": "SELECT COUNT(DISTINCT customer_unique_id) FROM olist_customers"},
    {"id": "S3", "type": "单表聚合", "question": "订单评价的平均评分是多少？",
     "sql": "SELECT ROUND(AVG(review_score), 2) FROM olist_order_reviews"},
    {"id": "S4", "type": "单表聚合", "question": "一共有多少条订单评价记录？",
     "sql": "SELECT COUNT(*) FROM olist_order_reviews"},
    {"id": "S5", "type": "单表聚合", "question": "商品表里一共有多少个商品？",
     "sql": "SELECT COUNT(*) FROM olist_products"},
    {"id": "S6", "type": "单表聚合", "question": "平台上一共有多少个卖家？",
     "sql": "SELECT COUNT(*) FROM olist_sellers"},
    {"id": "S7", "type": "单表聚合", "question": "被取消的订单有多少笔？",
     "sql": "SELECT COUNT(*) FROM olist_orders WHERE order_status = 'canceled'"},
    {"id": "S8", "type": "单表聚合", "question": "订单明细中商品的平均单价是多少？",
     "sql": "SELECT ROUND(AVG(price), 2) FROM olist_order_items"},

    # ---- 多表关联 (10) ----
    {"id": "M1", "type": "多表关联", "question": "全平台的总 GMV 是多少？",
     "sql": f"SELECT ROUND(SUM(oi.price), 2) FROM olist_order_items oi JOIN olist_orders o ON oi.order_id = o.order_id WHERE o.{NC}"},
    {"id": "M2", "type": "多表关联", "question": "整体客单价是多少？",
     "sql": f"SELECT ROUND(SUM(oi.price) / COUNT(DISTINCT o.order_id), 2) FROM olist_order_items oi JOIN olist_orders o ON oi.order_id = o.order_id WHERE o.{NC}"},
    {"id": "M3", "type": "多表关联", "question": "SP 州的 GMV 是多少？",
     "sql": f"SELECT ROUND(SUM(oi.price), 2) FROM olist_order_items oi JOIN olist_orders o ON oi.order_id = o.order_id JOIN olist_customers c ON o.customer_id = c.customer_id WHERE o.{NC} AND c.customer_state = 'SP'"},
    {"id": "M4", "type": "多表关联", "question": "用信用卡支付的金额合计是多少？",
     "sql": f"SELECT ROUND(SUM(pay.payment_value), 2) FROM olist_order_payments pay JOIN olist_orders o ON pay.order_id = o.order_id WHERE o.{NC} AND pay.payment_type = 'credit_card'"},
    {"id": "M5", "type": "多表关联", "question": "最热门的 5 个商品类目（按去重订单数）是哪些？",
     "sql": f"SELECT t.product_category_name_english, COUNT(DISTINCT oi.order_id) AS order_count FROM olist_order_items oi JOIN olist_products p ON oi.product_id = p.product_id JOIN olist_product_category_translation t ON p.product_category_name = t.product_category_name JOIN olist_orders o ON oi.order_id = o.order_id WHERE o.{NC} GROUP BY t.product_category_name_english ORDER BY order_count DESC LIMIT 5"},
    {"id": "M6", "type": "多表关联", "question": "非取消订单里各支付方式分别有多少笔支付记录？",
     "sql": f"SELECT pay.payment_type, COUNT(*) AS cnt FROM olist_order_payments pay JOIN olist_orders o ON pay.order_id = o.order_id WHERE o.{NC} GROUP BY pay.payment_type"},
    {"id": "M7", "type": "多表关联", "question": "RJ 州客户的订单总数是多少？",
     "sql": f"SELECT COUNT(DISTINCT o.order_id) FROM olist_orders o JOIN olist_customers c ON o.customer_id = c.customer_id WHERE o.{NC} AND c.customer_state = 'RJ'"},
    {"id": "M8", "type": "多表关联", "question": "有多少笔非取消订单是用 boleto 支付的？",
     "sql": f"SELECT COUNT(DISTINCT o.order_id) FROM olist_orders o JOIN olist_order_payments pay ON o.order_id = pay.order_id WHERE o.{NC} AND pay.payment_type = 'boleto'"},
    {"id": "M9", "type": "多表关联", "question": "已送达订单中评分大于等于 4 分的订单有多少笔？",
     "sql": "SELECT COUNT(DISTINCT o.order_id) FROM olist_orders o JOIN olist_order_reviews r ON o.order_id = r.order_id WHERE o.order_status = 'delivered' AND r.review_score >= 4"},
    {"id": "M10", "type": "多表关联", "question": "各英语类目的 GMV 排名前 5 是多少？",
     "sql": f"SELECT t.product_category_name_english, ROUND(SUM(oi.price), 2) AS gmv FROM olist_order_items oi JOIN olist_products p ON oi.product_id = p.product_id JOIN olist_product_category_translation t ON p.product_category_name = t.product_category_name JOIN olist_orders o ON oi.order_id = o.order_id WHERE o.{NC} GROUP BY t.product_category_name_english ORDER BY gmv DESC LIMIT 5"},

    # ---- 时序环比 (6) ----
    {"id": "T1", "type": "时序环比", "question": "2017 年黑五（11 月）的 GMV 是多少？",
     "sql": f"SELECT ROUND(SUM(oi.price), 2) FROM olist_order_items oi JOIN olist_orders o ON oi.order_id = o.order_id WHERE o.{NC} AND o.order_purchase_timestamp >= '2017-11-01' AND o.order_purchase_timestamp < '2017-12-01'"},
    {"id": "T2", "type": "时序环比", "question": "2017 年 10 月的 GMV 是多少？",
     "sql": f"SELECT ROUND(SUM(oi.price), 2) FROM olist_order_items oi JOIN olist_orders o ON oi.order_id = o.order_id WHERE o.{NC} AND o.order_purchase_timestamp >= '2017-10-01' AND o.order_purchase_timestamp < '2017-11-01'"},
    {"id": "T3", "type": "时序环比", "question": "2017 年 11 月一共有多少笔订单？",
     "sql": f"SELECT COUNT(*) FROM olist_orders o WHERE o.{NC} AND o.order_purchase_timestamp >= '2017-11-01' AND o.order_purchase_timestamp < '2017-12-01'"},
    {"id": "T4", "type": "时序环比", "question": "2017 年全年的 GMV 是多少？",
     "sql": f"SELECT ROUND(SUM(oi.price), 2) FROM olist_order_items oi JOIN olist_orders o ON oi.order_id = o.order_id WHERE o.{NC} AND o.order_purchase_timestamp >= '2017-01-01' AND o.order_purchase_timestamp < '2018-01-01'"},
    {"id": "T5", "type": "时序环比", "question": "2018 年每个月的订单数分别是多少？",
     "sql": f"SELECT MONTH(o.order_purchase_timestamp) AS m, COUNT(*) AS cnt FROM olist_orders o WHERE o.{NC} AND YEAR(o.order_purchase_timestamp) = 2018 GROUP BY m ORDER BY m"},
    {"id": "T6", "type": "时序环比", "question": "2018 年一共有多少笔订单？",
     "sql": f"SELECT COUNT(*) FROM olist_orders o WHERE o.{NC} AND YEAR(o.order_purchase_timestamp) = 2018"},

    # ---- 排名对比 (6) ----
    {"id": "R1", "type": "排名对比", "question": "GMV 最高的前 5 个州是哪些？",
     "sql": f"SELECT c.customer_state, ROUND(SUM(oi.price), 2) AS gmv FROM olist_order_items oi JOIN olist_orders o ON oi.order_id = o.order_id JOIN olist_customers c ON o.customer_id = c.customer_id WHERE o.{NC} GROUP BY c.customer_state ORDER BY gmv DESC LIMIT 5"},
    {"id": "R2", "type": "排名对比", "question": "订单量最多的前 5 个州是哪些？",
     "sql": f"SELECT c.customer_state, COUNT(DISTINCT o.order_id) AS order_count FROM olist_orders o JOIN olist_customers c ON o.customer_id = c.customer_id WHERE o.{NC} GROUP BY c.customer_state ORDER BY order_count DESC LIMIT 5"},
    {"id": "R3", "type": "排名对比", "question": "GMV 最高的前 5 个卖家是哪些？",
     "sql": f"SELECT oi.seller_id, ROUND(SUM(oi.price), 2) AS gmv FROM olist_order_items oi JOIN olist_orders o ON oi.order_id = o.order_id WHERE o.{NC} GROUP BY oi.seller_id ORDER BY gmv DESC LIMIT 5"},
    {"id": "R4", "type": "排名对比", "question": "订单量最多的前 5 个城市是哪些？",
     "sql": f"SELECT c.customer_city, COUNT(DISTINCT o.order_id) AS order_count FROM olist_orders o JOIN olist_customers c ON o.customer_id = c.customer_id WHERE o.{NC} GROUP BY c.customer_city ORDER BY order_count DESC LIMIT 5"},
    {"id": "R5", "type": "排名对比", "question": "自然人客户数最多的前 5 个州是哪些？",
     "sql": "SELECT customer_state, COUNT(DISTINCT customer_unique_id) AS cust FROM olist_customers GROUP BY customer_state ORDER BY cust DESC LIMIT 5"},
    {"id": "R6", "type": "排名对比", "question": "支付金额最高的前 5 笔订单是哪些？",
     "sql": "SELECT order_id, ROUND(SUM(payment_value), 2) AS total FROM olist_order_payments GROUP BY order_id ORDER BY total DESC LIMIT 5"},
]


# ------------------------------------------------------------------
# P1.5 调优杠杆（首轮抽测 26/30=86.7% 后，针对两个「可泛化真错」补强）
#   - M4：支付金额聚合漏了「非取消订单」默认口径 → 口径注入(EXTRA_DOC) + 示例选择(EXTRA_QA#1)
#   - R4：按字符串维度(城市)排名时 LLM 误以为要先枚举取值 → 示例选择(EXTRA_QA#2) 教它直接 GROUP BY
# 这两组示例刻意「不与抽测题逐字重合」，避免把抽测做成记忆题；正式留出集准确率以 P3.3 为准。
# T5(日期格式假阴性)、S4(COUNT(*) vs DISTINCT 口径歧义) 不在这里「修」，作为发现项带到 P3.1/P3.2。
# 用 --no-tune 可关闭本调优，复现首轮基线。
# ------------------------------------------------------------------
EXTRA_DOC = """
## 口径补充（P1.5 调优注入）
- 支付类聚合（payment_value 求和、按 payment_type 分组）同样要 JOIN olist_orders 并默认排除 canceled/unavailable，不要只在 olist_order_payments 单表上算。
- 按某个维度（州/城市/类目/卖家）排名取 TOP-N 时，直接 GROUP BY 该维度列 + ORDER BY 指标 DESC LIMIT N 即可；不需要、也不应该先去枚举该列的具体取值。
"""

EXTRA_QA = [
    {
        "question": "各支付方式的支付金额合计是多少？",
        "sql": f"SELECT pay.payment_type, ROUND(SUM(pay.payment_value), 2) AS total FROM olist_order_payments pay JOIN olist_orders o ON pay.order_id = o.order_id WHERE o.{NC} GROUP BY pay.payment_type",
    },
    {
        "question": "客户数最多的前 5 个城市是哪些？",
        "sql": "SELECT customer_city, COUNT(DISTINCT customer_unique_id) AS cust FROM olist_customers GROUP BY customer_city ORDER BY cust DESC LIMIT 5",
    },
]


# ------------------------------------------------------------------
def read_db_cfg() -> dict:
    cfg = {}
    for line in (ROOT / "config" / "db_ro.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def read_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("SILICONFLOW_API_KEY="):
            key = line.split("=", 1)[1].strip()
            if key and key != "your_key_here":
                return key
    raise SystemExit("Key 未就绪（.env 里 SILICONFLOW_API_KEY 未填）")


def connect(cfg: dict) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=cfg["host"], user=cfg["user"], password=cfg["password"],
        database=cfg["database"], port=int(cfg["port"]),
        read_timeout=60, connect_timeout=15,
    )


def norm_rows(rows) -> list[tuple]:
    """行级规范化：数值统一为 round(float,2)、字符串 strip、None 保留；行排序无关化。"""
    out = []
    for row in rows:
        cells = []
        for c in row:
            if c is None:
                cells.append(None)
            elif isinstance(c, (int, float, Decimal)):
                cells.append(round(float(c), 2))
            else:
                cells.append(str(c).strip())
        out.append(tuple(cells))
    return sorted(out, key=lambda t: tuple(("" if x is None else str(x)) for x in t))


def run_sql(conn, sql: str):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def is_select(sql: str) -> bool:
    s = sql.strip().lower().lstrip("( ").strip()
    return s.startswith("select") or s.startswith("with")


def build_vanna(key: str, cfg: dict, tune: bool = True):
    """干净重建 chroma → 三路训练 → sleep(2) → 重新实例化（遵 D5）。

    tune=True 时叠加 P1.5 调优杠杆（口径注入 EXTRA_DOC + 示例选择 EXTRA_QA）。
    """
    from openai import OpenAI
    from vanna.openai import OpenAI_Chat
    from vanna.chromadb import ChromaDB_VectorStore

    chroma_dir = ROOT / "chroma"
    shutil.rmtree(chroma_dir, ignore_errors=True)  # rm -rf 干净重建，避免脏段

    def new_client():
        return OpenAI(api_key=key, base_url="https://api.siliconflow.cn/v1", timeout=90, max_retries=2)

    class MyVanna(ChromaDB_VectorStore, OpenAI_Chat):
        def __init__(self, client=None, config=None):
            ChromaDB_VectorStore.__init__(self, config=config)
            OpenAI_Chat.__init__(self, client=client, config=config)

    vconfig = {"model": "deepseek-ai/DeepSeek-V3.2", "path": str(chroma_dir), "language": "中文"}

    vn = MyVanna(client=new_client(), config=vconfig)
    vn.connect_to_mysql(host=cfg["host"], dbname=cfg["database"], user=cfg["user"],
                        password=cfg["password"], port=int(cfg["port"]))

    ddls = load_ddl()
    for ddl in ddls:
        vn.train(ddl=ddl)
    print(f"[训练] 第1路 DDL：{len(ddls)} 条")

    doc = METRICS_CONTEXT.strip()
    if tune:
        doc += "\n" + EXTRA_DOC.strip()
    vn.train(documentation=doc)
    print(f"[训练] 第2路 指标口径文档：1 篇{'（含 P1.5 调优注入）' if tune else ''}")

    qa_pairs = QA_PAIRS + (EXTRA_QA if tune else [])
    for qa in qa_pairs:
        vn.train(question=qa["question"], sql=qa["sql"])
    print(f"[训练] 第3路 问答对：{len(qa_pairs)} 组")

    time.sleep(2)  # 给 HNSW 段落盘留窗口（D5）
    vn = MyVanna(client=new_client(), config=vconfig)
    vn.connect_to_mysql(host=cfg["host"], dbname=cfg["database"], user=cfg["user"],
                        password=cfg["password"], port=int(cfg["port"]))
    print("[训练] 重新实例化 MyVanna（持久化加载完成）")
    return vn


def selfcheck(cfg: dict) -> None:
    """仅执行 30 条参考 SQL，校验 ground truth 可执行且结果合理。"""
    conn = connect(cfg)
    print("===== SELF-CHECK：执行 30 条参考 SQL =====")
    bad = 0
    for q in QUESTIONS:
        try:
            rows = run_sql(conn, q["sql"])
            preview = norm_rows(rows)[:6]
            print(f"[{q['id']}|{q['type']}] OK rows={len(rows)} {preview}")
        except Exception as e:
            bad += 1
            print(f"[{q['id']}|{q['type']}] SQL ERROR {type(e).__name__}: {str(e)[:160]}")
    conn.close()
    print(f"===== self-check 完成：{len(QUESTIONS)-bad}/{len(QUESTIONS)} 参考 SQL 可执行 =====")
    if bad:
        raise SystemExit(f"有 {bad} 条参考 SQL 报错，请先修正 ground truth 再抽测")


def sample(cfg: dict, key: str, tune: bool = True) -> None:
    conn = connect(cfg)
    # 先算 ground truth
    truth: dict[str, list] = {}
    for q in QUESTIONS:
        truth[q["id"]] = norm_rows(run_sql(conn, q["sql"]))
    print(f"[ground-truth] 30 条参考 SQL 结果已缓存\n")

    vn = build_vanna(key, cfg, tune=tune)

    print("\n===== 30 题抽测（生成 SQL → 执行 → 结果比对）=====")
    results = []
    for i, q in enumerate(QUESTIONS, 1):
        qid, qtype, question = q["id"], q["type"], q["question"]
        rec = {"id": qid, "type": qtype, "question": question}
        try:
            import io
            import contextlib
            _buf = io.StringIO()
            with contextlib.redirect_stdout(_buf):  # 屏蔽 vanna 的 prompt 噪声，日志只留结论
                gen_sql = vn.generate_sql(question=question)
        except Exception as e:
            gen_sql = None
            rec.update(status="生成失败", detail=f"{type(e).__name__}: {str(e)[:160]}")
        rec["gen_sql"] = gen_sql
        if not gen_sql:
            rec.setdefault("status", "生成失败")
            results.append(rec)
            print(f"[{i:>2}/30 {qid}] ✗ {rec['status']}")
            continue
        if not is_select(gen_sql):
            rec.update(status="非SELECT被拒")
            results.append(rec)
            print(f"[{i:>2}/30 {qid}] ✗ 非 SELECT 被拒")
            continue
        try:
            gen_rows = norm_rows(run_sql(conn, gen_sql))
        except Exception as e:
            rec.update(status="执行失败", detail=f"{type(e).__name__}: {str(e)[:160]}")
            results.append(rec)
            print(f"[{i:>2}/30 {qid}] ✗ 执行失败 {rec.get('detail','')[:80]}")
            continue
        ok = (gen_rows == truth[qid])
        rec.update(status="通过" if ok else "结果不一致", gen_rows=gen_rows[:8], truth_rows=truth[qid][:8])
        results.append(rec)
        print(f"[{i:>2}/30 {qid}] {'✓' if ok else '✗ 结果不一致'} | {question}")
        if not ok:
            print(f"        参考: {truth[qid][:4]}")
            print(f"        生成: {gen_rows[:4]}")
            print(f"        genSQL: {gen_sql[:200]}")

    conn.close()
    report(results)


def report(results: list[dict]) -> None:
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "通过")
    print("\n" + "=" * 56)
    print(f"P1.5 临时抽测准确率：{passed}/{total} = {passed/total*100:.1f}%")
    print("=" * 56)
    # 分类别
    from collections import defaultdict
    by_type = defaultdict(lambda: [0, 0])
    for r in results:
        by_type[r["type"]][1] += 1
        if r["status"] == "通过":
            by_type[r["type"]][0] += 1
    print("分类别准确率：")
    for t, (p, n) in by_type.items():
        print(f"  {t}: {p}/{n} = {p/n*100:.0f}%")
    # bad case 归因
    print("\nbad case 归因：")
    for r in results:
        if r["status"] != "通过":
            print(f"  [{r['id']}|{r['type']}] {r['status']} — {r['question']}")
            if r.get("gen_sql"):
                print(f"       genSQL: {r['gen_sql'][:180]}")
    print(f"\n验收线：≥80% → {'达标 ✅' if passed/total >= 0.8 else '未达标，需调优 ❌'}")


if __name__ == "__main__":
    cfg = read_db_cfg()
    if "--selfcheck" in sys.argv:
        selfcheck(cfg)
    else:
        tune = "--no-tune" not in sys.argv
        print(f"[模式] 调优杠杆：{'开启（口径注入 + 示例选择）' if tune else '关闭（复现首轮基线）'}")
        sample(cfg, read_key(), tune=tune)
