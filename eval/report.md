# ChatBI 评估报告

> 生成时间：2026-10-07 18:17:37｜运行：`uv run python scripts/run_eval.py`
> 评估集：`eval/questions.yaml`（30 题留出集，与训练 QA 不逐字重合）
> 引擎：vanna RAG（三路训练）+ DeepSeek-V3.2 + 自纠错 1 轮 + 口径/空结果守卫
> 准确率口径：分母=30，分子=执行成功且**结果一致**（行级规范化比对，SQL 文本不计）

## 总准确率

**29/30 = 96.7%**

## 分类别准确率

| 题型 | 准确率 |
|---|---|
| 单表聚合 | 8/8 = 100% |
| 多表关联 | 10/10 = 100% |
| 时序环比 | 5/6 = 83% |
| 排名对比 | 6/6 = 100% |

## 自纠错 / 守卫贡献

- 经自纠错后成功：0 题
- 经口径守卫触发修正：0 题
- 经空结果守卫触发改写：0 题

## Bad case 归因

| id | 题型 | 状态 | 归因 |
|---|---|---|---|
| Q23 | 时序环比 | 结果不一致 | 结果不一致（值或表示差异，含比对器保守判定） |

### 明细
- **Q23（2018 年每个月的订单数分别是多少？）**：第 0 行不一致：参考 (1.0, 7187.0) vs 生成 ('2018-01', 7187.0)
  - 生成 SQL：`SELECT DATE_FORMAT(order_purchase_timestamp, '%Y-%m') AS order_month, COUNT(*) AS order_count FROM olist_orders WHERE order_status NOT IN ('canceled', 'unavailable') AND YEAR(order_purchase_timestamp)`

## 复现

```bash
uv run python scripts/train.py           # 构建/重建向量库（干净全量）
uv run python scripts/run_eval.py          # 跑分并重生成本报告
```

> 注：本报告为单次运行结果；因 LLM 生成非确定性，多次运行总准确率在 93.3~96.7% 之间波动。
> 比对器对「表示级差异」（如月份 '2018-01' vs 1）保守判不一致，只会低估准确率（见 comparator.py）。