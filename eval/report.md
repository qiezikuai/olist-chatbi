# ChatBI 评估报告（P3.3）

> 生成时间：2026-09-23 22:55:53｜运行：`uv run python scripts/run_eval.py`
> 评估集：`eval/questions.yaml`（30 题留出集，与训练 QA 不逐字重合）
> 引擎：vanna RAG（三路训练）+ DeepSeek-V3.2 + 自纠错 1 轮 + 口径/空结果守卫
> 准确率口径（PLAN_v2 第 7 节）：分母=30，分子=执行成功且**结果一致**（行级规范化比对，SQL 文本不计）

## 总准确率

**28/30 = 93.3%**

## 分类别准确率

| 题型 | 准确率 |
|---|---|
| 单表聚合 | 8/8 = 100% |
| 多表关联 | 9/10 = 90% |
| 时序环比 | 5/6 = 83% |
| 排名对比 | 6/6 = 100% |

## 自纠错 / 守卫贡献

- 经自纠错（P2.3）后成功：0 题
- 经口径守卫（P2.4）触发修正：1 题
- 经空结果守卫（P2.4）触发改写：0 题

## Bad case 归因

| id | 题型 | 状态 | 归因 |
|---|---|---|---|
| Q12 | 多表关联 | 结果不一致 | 结果不一致（值或表示差异，含比对器保守判定） |
| Q23 | 时序环比 | 结果不一致 | 结果不一致（值或表示差异，含比对器保守判定） |

### 明细
- **Q12（信用卡（credit_card）支付的总金额是多少？）**：第 0 行不一致：参考 (12350042.56,) vs 生成 (10803504.81,)
  - 生成 SQL：`SELECT ROUND(SUM(items.price), 2) AS total FROM olist_order_items items JOIN olist_orders o ON items.order_id = o.order_id WHERE o.order_status NOT IN ('canceled', 'unavailable') AND items.order_id IN`
- **Q23（2018 年每个月的订单数分别是多少？）**：第 0 行不一致：参考 (1.0, 7187.0) vs 生成 ('2018-01', 7187.0)
  - 生成 SQL：`SELECT DATE_FORMAT(order_purchase_timestamp, '%Y-%m') AS month, COUNT(*) AS order_count FROM olist_orders WHERE order_purchase_timestamp >= '2018-01-01' AND order_purchase_timestamp < '2019-01-01' AND`

## 复现

```bash
uv run python scripts/train_and_test.py   # 或 p1_5_sampling.py：训练向量库
uv run python scripts/run_eval.py          # 跑分并重生成本报告
```

> 注：LLM 生成非确定性，复跑准确率可能小幅波动；本报告为单次运行结果。
> 比对器对「表示级差异」（如月份 '2018-01' vs 1）保守判不一致，只会低估准确率（见 comparator.py）。