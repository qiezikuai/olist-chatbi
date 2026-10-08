"""知识层（单源）：三路训练材料 + 向量库构建流程。

三路材料：
  1. 9 表 DDL —— 取自 docs/schema.md（由 scripts/gen_schema_doc.py 从库生成，文档跟库走）
  2. 指标口径摘要 —— 转写自 docs/metrics.md（唯一口径事实源）
  3. 问答对 few-shot —— QA_PAIRS(10) + EXTRA_QA(2)；EXTRA_DOC 为口径补充注入

建库流程（build）时序为踩坑固化，勿"简化"（见 DECISIONS「chroma HNSW」条目）：
  rm -rf chroma/ → 连接 MySQL → 训练 → sleep(2) → 重新实例化（冷加载验证）
LLM 客户端必须注入自建 OpenAI client（base_url 指向 SiliconFlow）——
vanna 0.7.9 的 OpenAI_Chat 忽略 config["base_url"]，直接走 config 会把请求发到
api.openai.com（见 DECISIONS「base_url 被吞」条目）。

产物 chroma/ 为运行产物（.gitignore），供 chatbi/engine.py 冷加载。
"""
from __future__ import annotations

import contextlib
import io
import shutil
import time
from pathlib import Path

from chatbi.secrets import read_db_config, read_llm_key

ROOT = Path(__file__).resolve().parent.parent

# ---------- 第 1 路：DDL（从 schema.md 提取） ----------
def load_ddl() -> list[str]:
    schema_md = (ROOT / "docs" / "schema.md").read_text(encoding="utf-8")
    return [line for line in schema_md.splitlines() if line.startswith("CREATE TABLE")]


# ---------- 第 2 路：指标口径摘要（docs/metrics.md 的转写） ----------
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

# ---------- 第 3 路：问答对 few-shot（覆盖聚合/关联/时序/对比） ----------
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

# 调优补充材料（针对抽测暴露的两个可泛化真错：支付聚合口径、维度枚举误解）
NC = "order_status NOT IN ('canceled','unavailable')"  # 默认非取消口径

EXTRA_DOC = """
## 口径补充
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


DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3.2"


def new_vanna(client, chroma_dir: Path, model: str = DEFAULT_MODEL, dialect: str | None = None):
    """构造 vanna 组合实例（需外部注入带 base_url 的 OpenAI client，见模块注释）。

    dialect 会进入 vanna 的生成提示词（"You are a {dialect} expert"、要求输出
    {dialect}-compliant 的 SQL）。默认 None = 不传，vanna 内部回落为通用 "SQL"——
    Olist/sakila 的既有材料正是这么训的，改动它会改变已验证基线，故只在 sqlite
    数据集上显式传 "SQLite"。
    """
    from vanna.openai import OpenAI_Chat
    from vanna.chromadb import ChromaDB_VectorStore

    class MyVanna(ChromaDB_VectorStore, OpenAI_Chat):
        def __init__(self, client=None, config=None):
            ChromaDB_VectorStore.__init__(self, config=config)
            OpenAI_Chat.__init__(self, client=client, config=config)

    config = {"model": model, "path": str(chroma_dir), "language": "中文"}
    if dialect:
        config["dialect"] = dialect
    return MyVanna(client=client, config=config)


def new_llm_client(api_key: str):
    from openai import OpenAI
    return OpenAI(api_key=api_key, base_url="https://api.siliconflow.cn/v1",
                  timeout=90, max_retries=2)


def connect_mysql(vn, db_cfg: dict) -> None:
    vn.connect_to_mysql(host=db_cfg["host"], dbname=db_cfg["database"], user=db_cfg["user"],
                        password=db_cfg["password"], port=int(db_cfg["port"]))


def build(llm_key: str | None = None, db_cfg: dict | None = None,
          tune: bool = True, chroma_dir: str | Path | None = None,
          materials=None, dataset=None):
    """干净重建向量库：rm-rf → 三路训练 → sleep(2) → 重新实例化。返回可查询实例。

    materials=None（默认）：材料取本模块的 Olist 常量。
      tune=True（生产正源）：12 组问答对 + 口径摘要 + 口径补充。
      tune=False（基线复现）：仅 10 组问答对、无补充——知识库随之降级。
    materials=Materials：用外部给定的三路材料（其他数据集由 scripts/onboard.py 生成，
      经 chatbi/datasets.py 解析）。此时 tune 不起作用——材料已是定稿。
    dataset=Dataset：按该数据集的后端决定连接方式与生成方言。缺省解析为 olist。

    后端差异：mysql 训练前 connect_to_mysql（vanna 的 DB 连接只服务于它自带的 run_sql，
      本项目执行走自有只读闸）；sqlite 不连库——三路材料已含全部 DDL，且生成提示词经
      dialect="SQLite" 适配方言（vanna 的提示词是 "You are a {dialect} expert"）。
    """
    from chatbi.datasets import Materials, resolve

    key = llm_key or read_llm_key()
    ds = dataset or resolve()
    cfg = db_cfg if db_cfg is not None else (
        None if ds.backend == "sqlite" else read_db_config(ds.db_env_path))
    chroma_dir = Path(chroma_dir) if chroma_dir else ds.chroma_dir
    dialect = "SQLite" if ds.backend == "sqlite" else None

    if materials is None:
        materials = Materials(
            ddl=load_ddl(),
            documentation=METRICS_CONTEXT.strip() + ("\n" + EXTRA_DOC.strip() if tune else ""),
            qa_pairs=list(QA_PAIRS) + (list(EXTRA_QA) if tune else []),
        )
        doc_note = "（含口径补充）" if tune else ""
    else:
        doc_note = ""

    def new_instance():
        vn = new_vanna(new_llm_client(key), chroma_dir, dialect=dialect)  # 自建 client 注入 base_url
        if cfg is not None:
            connect_mysql(vn, cfg)
        return vn

    shutil.rmtree(chroma_dir, ignore_errors=True)      # ① 干净重建，避免脏段（勿删，见模块注释）

    vn = new_instance()

    # vanna 的 train() 会把每条 DDL 全文打印到 stdout，16 表即刷屏；
    # 屏蔽其输出、只保留本模块的进度行（同 graph.py 屏蔽 vanna prompt 噪声的做法）。
    with contextlib.redirect_stdout(io.StringIO()):
        for ddl in materials.ddl:
            vn.train(ddl=ddl)
    print(f"[训练] 第1路 DDL：{len(materials.ddl)} 条")

    if materials.documentation:
        with contextlib.redirect_stdout(io.StringIO()):
            vn.train(documentation=materials.documentation)
    print(f"[训练] 第2路 指标口径文档：{1 if materials.documentation else 0} 篇{doc_note}")

    with contextlib.redirect_stdout(io.StringIO()):
        for qa in materials.qa_pairs:
            vn.train(question=qa["question"], sql=qa["sql"])
    print(f"[训练] 第3路 问答对：{len(materials.qa_pairs)} 组")

    time.sleep(2)                                      # ③ 给 HNSW 段落盘留窗口
    vn = new_instance()                                # ④ 重新实例化（冷加载验证）
    print("[训练] 重新实例化完成（持久化可加载验证）")
    return vn
