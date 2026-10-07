# ChatBI 评估报告

> 生成时间：2026-10-07 17:02:19｜运行：`uv run python scripts/run_eval.py`
> 评估集：`eval/questions.yaml`（30 题留出集，与训练 QA 不逐字重合）
> 引擎：vanna RAG（三路训练）+ DeepSeek-V3.2 + 自纠错 1 轮 + 口径/空结果守卫
> 准确率口径：分母=30，分子=执行成功且**结果一致**（行级规范化比对，SQL 文本不计）

## 总准确率

**28/30 = 93.3%**

## 分类别准确率

| 题型 | 准确率 |
|---|---|
| 单表聚合 | 8/8 = 100% |
| 多表关联 | 10/10 = 100% |
| 时序环比 | 5/6 = 83% |
| 排名对比 | 5/6 = 83% |

## 自纠错 / 守卫贡献

- 经自纠错后成功：0 题
- 经口径守卫触发修正：0 题
- 经空结果守卫触发改写：0 题

## Bad case 归因

| id | 题型 | 状态 | 归因 |
|---|---|---|---|
| Q23 | 时序环比 | 结果不一致 | 结果不一致（值或表示差异，含比对器保守判定） |
| Q30 | 排名对比 | 结果不一致 | 结果不一致（值或表示差异，含比对器保守判定） |

### 明细
- **Q23（2018 年每个月的订单数分别是多少？）**：第 0 行不一致：参考 (1.0, 7187.0) vs 生成 ('2018-01', 7187.0)
  - 生成 SQL：`SELECT DATE_FORMAT(order_purchase_timestamp, '%Y-%m') AS month, COUNT(*) AS order_count FROM olist_orders WHERE order_status NOT IN ('canceled', 'unavailable') AND order_purchase_timestamp >= '2018-01`
- **Q30（支付金额最高的前 5 笔订单是哪些？）**：第 0 行不一致：参考 ('03caa2c082116e1d31e67e9ae3700499', 13664.08) vs 生成 ('03caa2c082116e1d31e67e9ae3700499', 13440.0)
  - 生成 SQL：`SELECT oi.order_id, ROUND(SUM(oi.price), 2) AS total_payment FROM olist_order_items oi JOIN olist_orders o ON oi.order_id = o.order_id WHERE o.order_status NOT IN ('canceled', 'unavailable') GROUP BY `

## 复现

```bash
uv run python scripts/train.py           # 构建/重建向量库（干净全量）
uv run python scripts/run_eval.py          # 跑分并重生成本报告
```

> 注：LLM 生成非确定性，复跑准确率可能小幅波动；本报告为单次运行结果。
> 比对器对「表示级差异」（如月份 '2018-01' vs 1）保守判不一致，只会低估准确率（见 comparator.py）。