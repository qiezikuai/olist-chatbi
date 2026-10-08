# data 库表结构（接入时从库实时提取）

> 由 `scripts/onboard.py` 于 2026-10-08 22:27 生成：DDL 取自 SHOW CREATE TABLE、行数为 COUNT(*) 实测、外键取自 information_schema。
> 共 1 张基表 / 99,441 行

## 表关系（外键约束，读自库、非推测）

该库未声明外键约束——JOIN 关系需人工确认后补进 metrics.md，否则多表问题易错。

| 表 | 行数 | 表注释 |
|---|---:|---|
| olist_orders_dataset | 99,441 | — |

## olist_orders_dataset（99,441 行）

```sql
CREATE TABLE "olist_orders_dataset" ("order_id" TEXT, "customer_id" TEXT, "order_status" TEXT, "order_purchase_timestamp" TEXT, "order_approved_at" TEXT, "order_delivered_carrier_date" TEXT, "order_delivered_customer_date" TEXT, "order_estimated_delivery_date" TEXT)
```
