# Olist 项目指标口径定义（P1.3 产物 · Agent 的唯一口径事实源）

> 本文档是 ChatBI 回答指标问题的**唯一口径依据**。评估集（30 题）的标准答案 SQL 必须与本文档一致。
> 锚点数字均经 2026-09-20 对库验证（chatbi_ro 只读执行），SQL 附后。

## 0. 全局约定

| 约定 | 内容 |
|---|---|
| 时间基准 | 一切时间分析用 `olist_orders.order_purchase_timestamp`（下单时间），不用审批/发货时间 |
| 数据范围 | 全量订单 99,441 笔（2016-09-04 ~ 2018-10-17）；**默认口径=非取消订单**（`order_status NOT IN ('canceled','unavailable')`）；涉及履约的题目用"已送达"口径（`order_status='delivered'`，96,478 笔） |
| 金额单位 | 巴西雷亚尔 BRL；GMV 用 `olist_order_items.price`，**运费 `freight_value` 不计入 GMV** |
| 客户识别 | 统计"人数/客户数"一律用 `olist_customers.customer_unique_id`（自然人）；`customer_id` 每单唯一，只用于关联订单 |
| 地区口径 | 用客户州 `olist_customers.customer_state`（两位州码：SP=圣保罗、RJ=里约、MG=米纳斯） |
| 类目口径 | 商品类目一律经翻译表 `olist_product_category_translation` 转英语类目 |
| 行数陷阱 | 订单 JOIN 明细（1:N）后 `COUNT(*)` 是"订单行数"不是"订单数"，数订单必须 `COUNT(DISTINCT order_id)`；`olist_geolocation` 关联前必须去重 |

## 1. GMV（商品成交总额）

- **定义**：`GMV = SUM(olist_order_items.price)`，默认排除 canceled/unavailable 订单。
- **粒度**：支持任意维度（月/州/类目/卖家）拆分，维度字段见 schema.md JOIN 图谱。
- **易错**：①不要用 `olist_orders` 表算 GMV（它没有金额）；②JOIN 明细后数订单要去重；③两种口径（下单/已送达）混用会导致数字对不上，回答时须声明所用口径。

## 2. 复购率

- **定义**：`复购率 = 下单≥2 次的自然人数 ÷ 全部下单自然人数`（全周期口径，不做时间窗口切分）。
- **锚点（已验证）**：复购客户 2,997 ÷ 自然人 96,096 = **3.12%**。
- **业务解读**：典型"拉新型"平台——96.88% 的客户终生只买 1 单，增长抓手在拉新质量与首单转化，而非复购运营。
- **延伸（客户分层）**：由于 F（频次）维度近乎失效（绝大多数人只有 1 单，无区分度），本项目客户价值分层弃用标准 RFM，改用 **R+M 两维**：R=最近一次下单距观测期期末的天数，M=累计消费金额（SUM(price)），各按五分位切档。

## 3. 客单价（AOV）

- **定义**：`客单价 = GMV ÷ 去重订单数`（每单均价口径）。
- **量价分解**：GMV 变化 = 订单量变化 × 客单价变化。验证案例：2017-11（黑五月）GMV 环比 +53% ≈ 订单 +63% × 客单价 −6%（1.63 × 0.94 ≈ 1.53）——增量来自订单放量、单笔金额摊薄。
- **易错**：不要把"人均消费金额（GMV÷人数）"与"客单价（GMV÷订单数）"混用；两者在复购场景差异明显，回答时声明口径。

## 4. 口径锚点 SQL（已验证）

```sql
-- 自然人数 / 复购率
SELECT COUNT(DISTINCT customer_unique_id) FROM olist_customers;                      -- 96,096
SELECT SUM(cnt >= 2) AS rep_customers,                                               -- 2,997
       ROUND(SUM(cnt >= 2) / COUNT(*) * 100, 2) AS rep_rate                          -- 3.12%
FROM (SELECT customer_unique_id, COUNT(DISTINCT customer_id) AS cnt
      FROM olist_customers GROUP BY customer_unique_id) t;

-- 订单数 / 已送达数
SELECT COUNT(*) FROM olist_orders;                                                   -- 99,441
SELECT COUNT(*) FROM olist_orders WHERE order_status = 'delivered';                  -- 96,478
```

## 5. 常见问法 → 口径映射（评估题命中的口径速查）

| 用户问法 | 应采用口径 |
|---|---|
| "GMV 多少 / 卖了多少" | SUM(price)，非取消订单 |
| "复购率 / 回头客比例" | 第 2 节全周期口径 |
| "客单价" | GMV ÷ 去重订单数 |
| "黑五表现 / 11 月环比" | order_purchase_timestamp 所在月，2017-11 vs 2017-10 |
| "哪个州/地区卖得好" | customer_state 维度 |
| "什么品类热门" | 类目经翻译表转英语 |
| "评价怎么样" | review_score 均值，同一订单多条评价按 review_id 去重 |
| "配送快不快 / 准时率" | 已送达口径，actual=order_delivered_customer_date vs estimated=order_estimated_delivery_date |
