# scripts/archive —— 历史一次性脚本

链路搭建期（冒烟、排障、临时抽测、早期训练）留下的脚本，保留作过程记录与复盘素材，**不是在用工具**：

| 脚本 | 当时用途 | 现行正源 |
|---|---|---|
| `smoke_test.py` | 首日链路冒烟（会向 chroma 追加早期简版样例） | `scripts/train.py` + `main.py` |
| `diag_api.py` | 定位 LLM 端点超时（绕过 vanna 直连验证） | 结论已沉淀至 `docs/DECISIONS.md` |
| `train_and_test.py` | 首次三路训练 + 随机抽 3 题试跑 | `chatbi/knowledge.py`（材料）+ `scripts/train.py`（重建） |
| `p1_5_sampling.py` | 30 题临时抽测 + 调优（含参考 SQL 自检） | 正式评估走 `eval/questions.yaml` + `scripts/run_eval.py` |

注意：
- 归档脚本运行时材料改从 `chatbi/knowledge.py` 取（或断言与正源一致），漂移即报错。
- 不要在已有向量库上运行 `smoke_test.py` / `train_and_test.py`（追加污染）；重建一律 `scripts/train.py`。
