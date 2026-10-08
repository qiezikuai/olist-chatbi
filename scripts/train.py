"""构建/重建向量库——生产唯一入口（实现在 chatbi/knowledge.py，行为单源）。

时序（干净重建 → 三路训练 → sleep(2) → 重实例化）与训练材料均在 knowledge.build 中固化，
勿在本脚本内另写流程。

运行：uv run python scripts/train.py
凭据约束：Key/DB 凭据经 chatbi/secrets 自读，绝不打印、绝不入 git（chroma/ 亦在 .gitignore）。
"""
import sys
from pathlib import Path

# 中文 Windows 控制台/管道下 stdout 用 GBK，✅ 等符号会触发 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # 支持从任意 cwd 运行

from chatbi import knowledge  # noqa: E402


def main() -> int:
    knowledge.build(tune=True)
    print("\n✅ 向量库已构建：chroma/（9 DDL + 指标口径文档 + 12 组问答对）")
    print("下一步：")
    print("  uv run python main.py                  # 端到端问数（3 个 demo 问题）")
    print("  uv run python scripts/run_eval.py      # 30 题评估跑分 → eval/report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
