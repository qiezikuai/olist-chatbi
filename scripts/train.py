"""构建/重建向量库——生产唯一入口（实现在 chatbi/knowledge.py，行为单源）。

时序（干净重建 → 三路训练 → sleep(2) → 重实例化）与训练材料均在 knowledge.build 中固化，
勿在本脚本内另写流程。

运行：
  uv run python scripts/train.py                      # 默认数据集 olist
  uv run python scripts/train.py --dataset sakila     # 用该数据集已落盘的材料重建

数据集的材料来源见 chatbi/datasets.py：olist 取 chatbi/knowledge.py 的常量，
其他数据集读 datasets/<name>/ 下的 schema.md / metrics.md / qa.yaml。
凭据约束：Key/DB 凭据经 chatbi/secrets 自读，绝不打印、绝不入 git（chroma/ 亦在 .gitignore）。
"""
import argparse
import sys
from pathlib import Path

# 中文 Windows 控制台/管道下 stdout 用 GBK，✅ 等符号会触发 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # 支持从任意 cwd 运行

from chatbi import knowledge                            # noqa: E402
from chatbi.datasets import list_datasets, load_materials, resolve   # noqa: E402
from chatbi.secrets import read_db_config               # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="构建/重建向量库")
    ap.add_argument("--dataset", default="olist",
                    help=f"数据集名（可用：{', '.join(list_datasets())}）")
    args = ap.parse_args()

    try:
        ds = resolve(args.dataset)
    except RuntimeError as e:
        print(f"数据集解析失败：{e}")
        return 2

    if ds.materials_dir is None:
        knowledge.build(tune=True)
        print("\n✅ 向量库已构建：chroma/（9 DDL + 指标口径文档 + 12 组问答对）")
        print("下一步：")
        print("  uv run python main.py                  # 端到端问数（3 个 demo 问题）")
        print("  uv run python scripts/run_eval.py      # 30 题评估跑分 → eval/report.md")
        return 0

    materials = load_materials(ds)
    knowledge.build(materials=materials,
                    db_cfg=read_db_config(ds.db_env_path),
                    chroma_dir=ds.chroma_dir)
    print(f"\n✅ 数据集 '{ds.name}' 向量库已构建：{ds.chroma_dir}")
    print(f"   材料：{len(materials.ddl)} 条 DDL｜口径文档 {len(materials.documentation)} 字"
          f"｜{len(materials.qa_pairs)} 组问答对")
    print("下一步：")
    print(f"  uv run python main.py --dataset {ds.name} \"你的问题\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
