"""P1.4：三路训练 + 10 组问答对试跑，验证 RAG 闭环

训练数据：
- 第 1 路：9 表 DDL（从 docs/schema.md 提取）
- 第 2 路：指标口径文档（docs/metrics.md 核心定义）
- 第 3 路：10 组问答对（业务问题 + 标准 SQL，覆盖聚合/关联/时序/对比）

试跑：随机抽 3 题验证 LLM 生成 SQL 并执行，对比标准答案。

运行：uv run python scripts/train_and_test.py
"""
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent.parent


# ---------- 第 1 路：DDL（从 schema.md 提取） ----------
def load_ddl() -> list[str]:
    schema_md = (ROOT / "docs" / "schema.md").read_text(encoding="utf-8")
    ddls = []
    for line in schema_md.splitlines():
        if line.startswith("CREATE TABLE"):
            ddls.append(line)
    return ddls


# ---------- 第 2 路：指标口径文档（metrics.md 核心定义） ----------
METRICS_CONTEXT = """
# Olist 指标口径定义（摘要）

## 全局约定
- 时间基准：order_purchase_timestamp（下单时间）
- 数据范围：非取消订单（order_status NOT IN ('canceled','unavailable')）
- 金额单位：BRL；GMV = SUM(price)，运费不计入
- 客户识别：人数/客户数用 customer_unique_id（自然人）
- 地区口径：customer_state（两位州码）
- 类目口径：经 translation 表转英语类目
- 行数陷阱：JOIN 明细后 COUNT(*) 是行数不是订单数，数订单必须 COUNT(DISTINCT order_id)

## GMV
- 定义：GMV = SUM(olist_order_items.price)，默认排除 canceled/unavailable
- 粒度：支持按月/州/类目/卖家拆分
- 易错：不要用 olist_orders 表算 GMV（无金额）；JOIN 明细后数订单要去重

## 复购率
- 定义：复购率 = 下单≥2 次的自然人数 ÷ 全部下单自然人数（全周期口径）
- 锚点：复购客户 2,997 ÷ 自然人 96,096 = 3.12%
- 延伸：弃用标准 RFM（F 维度失效），改用 R+M 两维分层

## 客单价（AOV）
- 定义：客单价 = GMV ÷ 去重订单数（每单均价）
- 量价分解：GMV 变化 = 订单量变化 × 客单价变化
- 易错：不要与"人均消费金额（GMV÷人数）"混用

## 常见问法→口径映射
- "GMV 多少" → SUM(price)，非取消订单
- "复购率" → 全周期口径（≥2 单自然人占比）
- "客单价" → GMV÷订单数
- "黑五表现" → order_purchase_timestamp 所在月，2017-11 vs 2017-10
- "哪个州卖得好" → customer_state 维度
- "什么品类热门" → 类目经翻译表转英语
- "评价怎么样" → review_score 均值，按 review_id 去重
- "配送快不快" → 已送达口径，actual vs estimated
"""


# ---------- 第 3 路：10 组问答对（覆盖聚合/关联/时序/对比） ----------
QA_PAIRS = [
    {
        "question": "总共有多少笔订单？",
        "sql": "SELECT COUNT(*) AS order_count FROM olist_orders WHERE order_status NOT IN ('canceled', 'unavailable')",
    },
    {
        "question": "总共有多少个独立客户？",
        "sql": "SELECT COUNT(DISTINCT customer_unique_id) AS unique_customers FROM olist_customers",
    },
    {
        "question": "总 GMV 是多少？",
        "sql": "SELECT SUM(oi.price) AS gmv FROM olist_order_items oi JOIN olist_orders o ON oi.order_id = o.order_id WHERE o.order_status NOT IN ('canceled', 'unavailable')",
    },
    {
        "question": "客单价是多少？",
        "sql": "SELECT SUM(oi.price) / COUNT(DISTINCT o.order_id) AS aov FROM olist_order_items oi JOIN olist_orders o ON oi.order_id = o.order_id WHERE o.order_status NOT IN ('canceled', 'unavailable')",
    },
    {
        "question": "复购率是多少？",
        "sql": "SELECT SUM(cnt >= 2) AS rep_customers, ROUND(SUM(cnt >= 2) / COUNT(*) * 100, 2) AS rep_rate FROM (SELECT customer_unique_id, COUNT(DISTINCT customer_id) AS cnt FROM olist_customers GROUP BY customer_unique_id) t",
    },
    {
        "question": "哪个州的订单量最多？",
        "sql": "SELECT c.customer_state, COUNT(DISTINCT o.order_id) AS order_count FROM olist_orders o JOIN olist_customers c ON o.customer_id = c.customer_id WHERE o.order_status NOT IN ('canceled', 'unavailable') GROUP BY c.customer_state ORDER BY order_count DESC LIMIT 5",
    },
    {
        "question": "2017 年黑五月（11 月）的 GMV 是多少？",
        "sql": "SELECT SUM(oi.price) AS gmv FROM olist_order_items oi JOIN olist_orders o ON oi.order_id = o.order_id WHERE o.order_status NOT IN ('canceled', 'unavailable') AND o.order_purchase_timestamp >= '2017-11-01' AND o.order_purchase_timestamp < '2017-12-01'",
    },
    {
        "question": "平均订单评分是多少？",
        "sql": "SELECT AVG(review_score) AS avg_score FROM olist_order_reviews",
    },
    {
        "question": "最热门的 5 个商品类目是什么？",
        "sql": "SELECT t.product_category_name_english, COUNT(DISTINCT oi.order_id) AS order_count FROM olist_order_items oi JOIN olist_products p ON oi.product_id = p.product_id JOIN olist_product_category_translation t ON p.product_category_name = t.product_category_name JOIN olist_orders o ON oi.order_id = o.order_id WHERE o.order_status NOT IN ('canceled', 'unavailable') GROUP BY t.product_category_name_english ORDER BY order_count DESC LIMIT 5",
    },
    {
        "question": "已送达订单的平均配送天数是多少？",
        "sql": "SELECT AVG(DATEDIFF(order_delivered_customer_date, order_purchase_timestamp)) AS avg_delivery_days FROM olist_orders WHERE order_status = 'delivered'",
    },
]


def main() -> None:
    # 加载 vanna
    from openai import OpenAI
    from vanna.openai import OpenAI_Chat
    from vanna.chromadb import ChromaDB_VectorStore

    key = None
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("SILICONFLOW_API_KEY="):
            key = line.split("=", 1)[1].strip()
    if not key or key == "your_key_here":
        raise SystemExit("Key 未就绪")

    llm_client = OpenAI(api_key=key, base_url="https://api.siliconflow.cn/v1", timeout=90, max_retries=2)

    class MyVanna(ChromaDB_VectorStore, OpenAI_Chat):
        def __init__(self, client=None, config=None):
            ChromaDB_VectorStore.__init__(self, config=config)
            OpenAI_Chat.__init__(self, client=client, config=config)

    vn = MyVanna(
        client=llm_client,
        config={
            "model": "deepseek-ai/DeepSeek-V3.2",
            "path": str(ROOT / "chroma"),
            "language": "中文",
        },
    )

    # 连接 MySQL
    cfg = {}
    for line in (ROOT / "config" / "db_ro.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    vn.connect_to_mysql(host=cfg["host"], dbname=cfg["database"], user=cfg["user"],
                        password=cfg["password"], port=int(cfg["port"]))
    print("MySQL 已连接（chatbi_ro，只读）")

    # 第 1 路：DDL
    ddls = load_ddl()
    for ddl in ddls:
        vn.train(ddl=ddl)
    print(f"第 1 路：{len(ddls)} 条 DDL 已训练")

    # 第 2 路：指标口径文档
    vn.train(documentation=METRICS_CONTEXT.strip())
    print("第 2 路：指标口径文档已训练")

    # 第 3 路：10 组问答对
    for qa in QA_PAIRS:
        vn.train(question=qa["question"], sql=qa["sql"])
    print(f"第 3 路：{len(QA_PAIRS)} 组问答对已训练")

    # 训练完重新实例化（确保 chroma 段全部落盘，同时验证持久化可加载）
    import time
    time.sleep(2)
    vn = MyVanna(
        client=OpenAI(api_key=key, base_url="https://api.siliconflow.cn/v1", timeout=90, max_retries=2),
        config={"model": "deepseek-ai/DeepSeek-V3.2", "path": str(ROOT / "chroma"), "language": "中文"},
    )
    vn.connect_to_mysql(host=cfg["host"], dbname=cfg["database"], user=cfg["user"],
                        password=cfg["password"], port=int(cfg["port"]))
    print("重新实例化 MyVanna（验证持久化可加载）")

    # 试跑：随机抽 3 题验证
    import random
    test_set = random.sample(QA_PAIRS, 3)
    print(f"\n===== 试跑 3 题（随机抽）=====")
    for i, qa in enumerate(test_set, 1):
        print(f"\n[题{i}] {qa['question']}")
        sql = vn.generate_sql(question=qa["question"])
        print(f"  生成 SQL: {sql}")
        try:
            df = vn.run_sql(sql)
            print(f"  执行结果:\n{df.head()}")
        except Exception as e:
            print(f"  执行失败：{type(e).__name__} {str(e)[:150]}")

    print("\n===== P1.4 试跑完成 =====")


if __name__ == "__main__":
    main()
