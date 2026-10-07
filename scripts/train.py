"""构建/重建向量库（三路训练 + 调优材料）。

干净重建 chroma → 训练 9 DDL + 指标口径文档 + 12 组问答对 → sleep(2) → 重实例化
（时序依据见 DECISIONS.md「chroma HNSW」决策：避免训练后立即查询的竞态/脏段）。
本脚本只产出向量库、不跑生成与评估；产物 chroma/ 供 main.py 与 run_eval.py 冷加载。

运行：uv run python scripts/train.py
凭据约束：Key/DB 凭据由脚本自读，绝不打印、绝不入 git（chroma/ 亦在 .gitignore）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 复用训练实现，保证训练材料单源（不重复维护口径/示例）
from p1_5_sampling import build_vanna, read_db_cfg, read_key  # noqa: E402


def main() -> int:
    cfg = read_db_cfg()
    key = read_key()
    build_vanna(key, cfg, tune=True)   # 干净重建 + 三路训练（含调优材料）
    print("\n✅ 向量库已构建：chroma/（9 DDL + 指标口径文档 + 12 组问答对）")
    print("下一步：")
    print("  uv run python main.py                  # 端到端问数（3 个 demo 问题）")
    print("  uv run python scripts/run_eval.py      # 30 题评估跑分 → eval/report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
