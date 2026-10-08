"""一键接入新 MySQL 库（薄 CLI；实现正源在 chatbi/onboarding.py）。

五步流程、自动化边界与凭据约束见 chatbi/onboarding.py 模块注释。

用法：
  uv run python scripts/onboard.py --dataset sakila --db-env config/db_sakila.env
  uv run python scripts/onboard.py                 # 全交互式输入连接信息
"""
import argparse
import sys
from pathlib import Path

# 中文 Windows 控制台/管道下 stdout 用 GBK，✓ 等符号会触发 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # 支持从任意 cwd 运行

from chatbi.onboarding import DEFAULT_QA_COUNT, run   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="一键接入新 MySQL 库（生成材料 + 训练 + 试跑）")
    ap.add_argument("--dataset", help="数据集名（小写字母/数字/下划线/连字符）；缺省用库名")
    ap.add_argument("--db-env", help="从该 env 文件读连接信息（缺省则交互式输入）")
    ap.add_argument("--qa-count", type=int, default=DEFAULT_QA_COUNT,
                    help=f"生成的问答对组数（默认 {DEFAULT_QA_COUNT}）")
    ap.add_argument("--skip-trial", action="store_true", help="跳过第 ⑤ 步试跑")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
