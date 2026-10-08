"""一键接入新数据源（薄 CLI；实现正源在 chatbi/onboarding.py）。

两种来源：
  --db-env <env>   接一个已有的 MySQL 库（需最小权限只读账号）
  --csv <文件>     接一个 CSV 文件（自动建表导入 SQLite，无需任何数据库账号）

五步流程、自动化边界与凭据约束见 chatbi/onboarding.py 模块注释。

用法：
  uv run python scripts/onboard.py --dataset sakila --db-env config/db_sakila.env
  uv run python scripts/onboard.py --csv data.csv --dataset mydata
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
    ap = argparse.ArgumentParser(description="一键接入新数据源（MySQL 库或 CSV 文件）")
    ap.add_argument("--dataset", help="数据集名（小写字母/数字/下划线/连字符）；缺省用库名或文件名")
    ap.add_argument("--db-env", help="从该 env 文件读 MySQL 连接信息（缺省则交互式输入）")
    ap.add_argument("--csv", help="CSV 文件路径：自动建表导入 SQLite 后接入（与 --db-env 二选一）")
    ap.add_argument("--header-names", help="无表头 CSV 的列名，逗号分隔（如 a,b,c）；不给则自动判定")
    ap.add_argument("--qa-count", type=int, default=DEFAULT_QA_COUNT,
                    help=f"生成的问答对组数（默认 {DEFAULT_QA_COUNT}）")
    ap.add_argument("--skip-trial", action="store_true", help="跳过第 ⑤ 步试跑")
    args = ap.parse_args()
    if args.csv and args.db_env:
        print("✗ --csv 与 --db-env 二选一，不能同时给")
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
