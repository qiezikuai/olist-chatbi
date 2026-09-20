"""P1.2：从 MySQL 拉取 9 表真实 DDL 与行数，合并中文注释，生成 docs/schema.md

设计要点：
- DDL 与行数始终取自数据库（SHOW CREATE TABLE / COUNT），文档不会与库漂移；
- 中文注释来自本脚本内的静态字典（人工维护的领域知识）；
- 有 DDL 列未被注释覆盖时打印警告，防止 schema 变化被静默忽略。

运行：uv run python scripts/gen_schema_doc.py
产出：docs/schema.md
"""
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent.parent

# ---------- 领域注释（人工维护） ----------
TABLE_NOTES = {
    "olist_orders": "订单主表：一行 = 一笔订单。含订单状态机（created→approved→invoiced→processing→shipped→delivered / canceled / unavailable）与各时间节点。",
    "olist_customers": "客户表：一行 = 一笔订单的客户上下文。注意 customer_id 每单唯一，识别『同一个人』必须用 customer_unique_id（复购分析的基石）。",
    "olist_order_items": "订单明细：一行 = 订单内一个商品条目，与 orders 是 1:N（一单多商品会多行）。price 为商品价，freight_value 为运费，两者分开。",
    "olist_order_payments": "支付记录：一行 = 一笔订单的一种支付方式；分期付款订单会有多行（payment_sequential 为序号）。",
    "olist_order_reviews": "评价表：一行 = 一条评价。注意同一订单可能有多条评价（问卷多次触发），按订单聚合前需去重。",
    "olist_products": "商品表：一行 = 一个商品。product_category_name 为葡语类目，翻译表见 olist_product_category_translation。",
    "olist_sellers": "卖家表：一行 = 一个卖家，含地理信息。",
    "olist_geolocation": "地理坐标表：一行 = 一个 zip 前缀的坐标点。同一 zip 前缀会有大量重复行（不同坐标），**与其他表关联前必须按 zip 前缀去重**，否则 JOIN 会爆炸式膨胀行数。",
    "olist_product_category_translation": "类目翻译表：葡语类目名 → 英语类目名，71 行，做类目分析务必走这张表翻译。",
}

COLUMN_NOTES = {
    "olist_orders": {
        "order_id": "订单唯一标识（主键）",
        "customer_id": "客户标识，每单唯一；关联 olist_customers.customer_id",
        "order_status": "订单状态：created/approved/invoiced/processing/shipped/delivered/canceled/unavailable",
        "order_purchase_timestamp": "下单时间（本项目一切时间分析的基准时间）",
        "order_approved_at": "支付审批通过时间",
        "order_delivered_carrier_date": "交给承运商时间",
        "order_delivered_customer_date": "客户签收时间（分析'已送达订单'用此字段判非空）",
        "order_estimated_delivery_date": "预计送达时间（与实际送达对比=履约准时率）",
    },
    "olist_customers": {
        "customer_id": "每单唯一的客户标识（主键，等于订单粒度）",
        "customer_unique_id": "自然人的唯一标识（跨单不变）；复购/客户数统计一律用它",
        "customer_zip_code_prefix": "客户邮编前 5 位（关联 geolocation 需先去重）",
        "customer_city": "客户城市",
        "customer_state": "客户所在州（地区口径的基准字段，如 SP/RJ/MG）",
    },
    "olist_order_items": {
        "order_id": "关联 olist_orders",
        "order_item_id": "同一订单内的商品序号（从 1 递增）",
        "product_id": "关联 olist_products",
        "seller_id": "关联 olist_sellers",
        "shipping_limit_date": "卖家最晚发货期限",
        "price": "商品成交价（BRL）。本项目 GMV 口径 = SUM(price)，运费不计入 GMV",
        "freight_value": "该条目的运费（分摊到件），单独统计不并入 GMV",
    },
    "olist_order_payments": {
        "order_id": "关联 olist_orders",
        "payment_sequential": "同一订单第几种支付方式（1 起）",
        "payment_type": "支付方式：credit_card/boleto(巴西票据)/voucher/debit_card/not_defined",
        "payment_installments": "分期期数（0/1=不分期）",
        "payment_value": "该笔支付的金额（BRL）",
    },
    "olist_order_reviews": {
        "review_id": "评价唯一标识",
        "order_id": "关联 olist_orders（一单可有多条评价，聚合前按 review_id 去重）",
        "review_score": "评分 1-5（口碑分析核心字段）",
        "review_comment_title": "评价标题（葡语，大量为空）",
        "review_comment_message": "评价正文（葡语，大量为空）",
        "review_creation_date": "问卷发送时间",
        "review_answer_timestamp": "客户作答时间",
    },
    "olist_products": {
        "product_id": "商品唯一标识（主键）",
        "product_category_name": "葡语类目名（翻译用 olist_product_category_translation）",
        "product_name_length": "商品名长度（字符数）",
        "product_description_length": "商品描述长度（字符数）",
        "product_photos_qty": "商品图片数量",
        "product_weight_g": "重量（克）",
        "product_length_cm": "长（厘米）",
        "product_height_cm": "高（厘米）",
        "product_width_cm": "宽（厘米）",
    },
    "olist_sellers": {
        "seller_id": "卖家唯一标识（主键）",
        "seller_zip_code_prefix": "卖家邮编前 5 位",
        "seller_city": "卖家城市",
        "seller_state": "卖家所在州",
    },
    "olist_geolocation": {
        "geolocation_zip_code_prefix": "邮编前 5 位（同前缀多行，先去重再关联）",
        "geolocation_lat": "纬度",
        "geolocation_lng": "经度",
        "geolocation_city": "城市",
        "geolocation_state": "州",
    },
    "olist_product_category_translation": {
        "product_category_name": "葡语类目名",
        "product_category_name_english": "英语类目名（对外报表用英语类目）",
    },
}

JOIN_HINTS = """-- 核心关联路径（SQL 生成时的标准 JOIN 图谱）
olist_orders o
  JOIN olist_customers c      ON o.customer_id = c.customer_id          -- 订单→客户（含州/城市）
  JOIN olist_order_items oi   ON oi.order_id = o.order_id               -- 订单→明细（1:N，GMV 在这里）
  JOIN olist_products p       ON oi.product_id = p.product_id           -- 明细→商品
  JOIN olist_product_category_translation t
                              ON p.product_category_name = t.product_category_name  -- 类目英译
  LEFT JOIN olist_sellers s   ON oi.seller_id = s.seller_id             -- 明细→卖家
  LEFT JOIN olist_order_payments pay ON pay.order_id = o.order_id       -- 订单→支付（1:N）
  LEFT JOIN olist_order_reviews r    ON r.order_id = o.order_id         -- 订单→评价（1:N）
-- ⚠ olist_geolocation 不要直接 JOIN：先 SELECT DISTINCT zip 前缀再去匹配
-- ⚠ 一单多商品时 oi 会放大行数：统计"订单数/客户数"不要经过 JOIN oi 后 COUNT(*)"""


def main() -> None:
    cfg = {}
    for line in (ROOT / "config" / "db_ro.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()

    conn = pymysql.connect(
        host=cfg["host"], port=int(cfg["port"]), user=cfg["user"],
        password=cfg["password"], database=cfg["database"], charset="utf8mb4",
    )
    cur = conn.cursor()
    cur.execute("SHOW TABLES")
    tables = sorted(r[0] for r in cur.fetchall())

    lines = [
        "# Olist 数仓 Schema（P1.2 生成物）",
        "",
        "> 本文件由 `scripts/gen_schema_doc.py` 从数据库自动生成（DDL 与行数实时拉取），",
        "> 中文注释人工维护。**改库后必须重新运行脚本**，训练材料以本文件为准。",
        f"> 生成时间：2026-09-20｜账号：{cfg['user']}（只读）｜库：{cfg['database']}",
        "",
        "## 0. 表关系与 JOIN 图谱",
        "",
        "```sql",
        JOIN_HINTS,
        "```",
        "",
        "| 表 | 行数 | 一句话说明 |",
        "|---|---:|---|",
    ]

    row_counts = {}
    for t in tables:
        cur.execute(f"SELECT COUNT(*) FROM `{t}`")
        row_counts[t] = cur.fetchone()[0]
        lines.append(f"| {t} | {row_counts[t]:,} | {TABLE_NOTES.get(t, 'TODO')} |")

    lines.append("")
    warn = 0
    for t in tables:
        cur.execute(f"SHOW CREATE TABLE `{t}`")
        ddl = cur.fetchone()[1]
        lines += ["", f"## {t}（{row_counts[t]:,} 行）", "", f"**说明**：{TABLE_NOTES.get(t, 'TODO')}", "", "```sql", ddl, "```", "", "### 列说明", ""]
        for col, note in COLUMN_NOTES.get(t, {}).items():
            lines.append(f"- `{col}`：{note}")
        # 校验：DDL 里的列是否都被注释覆盖
        import re
        ddl_cols = set(re.findall(r"^\s+`(\w+)`", ddl, re.M))
        noted = set(COLUMN_NOTES.get(t, {}))
        missing = ddl_cols - noted - {"pk_dummy"}
        if missing:
            warn += len(missing)
            lines.append(f"- ⚠️ 未注释列：{', '.join(sorted(missing))}")
        extra = noted - ddl_cols
        if extra:
            warn += len(extra)
            lines.append(f"- ⚠️ 注释了但 DDL 中不存在的列（请修正 COLUMN_NOTES）：{', '.join(sorted(extra))}")

    conn.close()
    out = ROOT / "docs" / "schema.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"生成 {out}（{len(tables)} 表，合计 {sum(row_counts.values()):,} 行，注释缺口警告 {warn} 条）")


if __name__ == "__main__":
    main()
