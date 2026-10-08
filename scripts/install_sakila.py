"""安装 MySQL 官方示例库 sakila，并建专用只读账号（跨领域验证用的第二个库）。

为什么必须走 mysql 客户端而非 pymysql：sakila-schema.sql 含 14 处 `DELIMITER`
与触发器/视图/存储过程，而 `DELIMITER` 是 mysql 客户端指令、不是 SQL 语句，
pymysql 逐条执行会直接语法报错。

用法（在自己的终端执行，root 密码交互输入、不回显、不落日志）：
    uv run python scripts/install_sakila.py            # 已存在 sakila 库时拒绝执行
    uv run python scripts/install_sakila.py --force    # 明知会重建仍继续

行为：
  1. 定位 mysql 客户端；SQL 文件缺失则从 MySQL 官方下载 sakila-db.zip 并解压
  2. 用 root 检查 sakila 是否已存在（schema 文件含 DROP SCHEMA，会清空重建）
  3. 经临时选项文件把 root 密码交给 mysql（不进命令行参数），依次载入 schema 与 data
  4. 建 chatbi_ro_sakila@localhost，仅 SELECT ON sakila.*，凭据写 config/db_sakila.env
  5. 用该只读账号复核表数与行数，并实证写操作被拒

账号不与 Olist 的 chatbi_ro 共用：最小权限、按库隔离（见 DECISIONS D3）。
"""
from __future__ import annotations

import argparse
import getpass
import glob
import os
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pymysql

# 中文 Windows 控制台/管道下 stdout 用 GBK，✓ 等符号会触发 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "datasets" / "sakila" / "sql"
SCHEMA_SQL = SQL_DIR / "sakila-db" / "sakila-schema.sql"
DATA_SQL = SQL_DIR / "sakila-db" / "sakila-data.sql"
ENV_OUT = ROOT / "config" / "db_sakila.env"

SAKILA_ZIP_URL = "https://downloads.mysql.com/docs/sakila-db.zip"
DB_NAME = "sakila"
RO_USER = "chatbi_ro_sakila"
HOST = "127.0.0.1"
PORT = 3306


def find_mysql_client() -> str:
    """定位 mysql 客户端可执行文件。"""
    found = shutil.which("mysql")
    if found:
        return found
    for pattern in (r"C:\Program Files\MySQL\MySQL Server *\bin\mysql.exe",
                    r"C:\Program Files (x86)\MySQL\MySQL Server *\bin\mysql.exe"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]      # 多版本共存时取字典序最大（通常即最新）
    raise RuntimeError("未找到 mysql 客户端：请确认 MySQL 已安装，或把其 bin 目录加入 PATH")


def ensure_sql_files() -> None:
    """SQL 文件缺失则下载官方 zip 并解压（不覆盖已有文件）。"""
    if SCHEMA_SQL.exists() and DATA_SQL.exists():
        print(f"[1/5] SQL 文件已就位：{SCHEMA_SQL.parent}")
        return
    SQL_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = SQL_DIR / "sakila-db.zip"
    print(f"[1/5] 下载 sakila 示例库：{SAKILA_ZIP_URL}")
    import urllib.request
    urllib.request.urlretrieve(SAKILA_ZIP_URL, zip_path)
    print(f"      解压 → {SQL_DIR}")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(SQL_DIR)
    if not (SCHEMA_SQL.exists() and DATA_SQL.exists()):
        raise RuntimeError(f"解压后未找到预期的 SQL 文件：{SCHEMA_SQL}")


def _mysql_option_file(user: str, password: str,
                       host: str = HOST, port: int = PORT) -> str:
    """写临时 mysql 选项文件承载连接参数，返回路径。

    用选项文件而非 `-p<密码>`：后者会让密码出现在进程命令行参数里，
    同机任何用户用 tasklist/ps 都能看到。选项文件权限收紧并在用完后删除。
    host/port 显式写入，避免 Windows 上 localhost 被解析成命名管道。
    """
    fd, path = tempfile.mkstemp(prefix="chatbi_mysql_", suffix=".cnf")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        esc = password.replace("\\", "\\\\").replace('"', '\\"')
        f.write(f'[client]\nhost="{host}"\nport={port}\nuser="{user}"\npassword="{esc}"\n')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass   # Windows 上 chmod 作用有限，文件本身在用户临时目录且用后即删
    return path


def load_via_cli(mysql_exe: str, root_pwd: str, sql_file: Path, label: str) -> None:
    """用 mysql 客户端执行一个 SQL 文件（支持 DELIMITER / 触发器 / 存储过程）。"""
    cnf = _mysql_option_file("root", root_pwd)
    try:
        with open(sql_file, "rb") as stdin:
            proc = subprocess.run(
                [mysql_exe, f"--defaults-extra-file={cnf}",
                 "--default-character-set=utf8mb4"],
                stdin=stdin, capture_output=True,
            )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"载入 {label} 失败（exit={proc.returncode}）：{err[:600]}")
        warn = proc.stderr.decode("utf-8", errors="replace").strip()
        if warn:
            print(f"      {label} 客户端提示：{warn[:200]}")
    finally:
        Path(cnf).unlink(missing_ok=True)


def create_readonly_account(root_pwd: str) -> str:
    """建/重置 chatbi_ro_sakila（仅 SELECT ON sakila.*），返回随机密码。"""
    ro_pwd = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
    conn = pymysql.connect(host=HOST, port=PORT, user="root", password=root_pwd)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE USER IF NOT EXISTS '{RO_USER}'@'localhost' IDENTIFIED BY %s", (ro_pwd,))
            cur.execute(f"ALTER USER '{RO_USER}'@'localhost' IDENTIFIED BY %s", (ro_pwd,))
            try:
                cur.execute(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM '{RO_USER}'@'localhost'")
            except pymysql.err.OperationalError:
                pass   # 无历史授权时报错属预期
            cur.execute(f"GRANT SELECT ON {DB_NAME}.* TO '{RO_USER}'@'localhost'")
            cur.execute("FLUSH PRIVILEGES")
        conn.commit()
    finally:
        conn.close()
    return ro_pwd


def verify_readonly(ro_pwd: str) -> tuple[int, int]:
    """用只读账号复核：表数、总行数，并实证写操作被拒。"""
    conn = pymysql.connect(host=HOST, port=PORT, user=RO_USER, password=ro_pwd,
                           database=DB_NAME, charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
            tables = [r[0] for r in cur.fetchall()]
            total = 0
            for t in tables:
                cur.execute(f"SELECT COUNT(*) FROM `{t}`")
                total += cur.fetchone()[0]
            try:
                cur.execute(f"DELETE FROM {DB_NAME}.actor WHERE actor_id = -1")
                raise RuntimeError("只读校验失败：DELETE 竟被允许，账号权限过宽")
            except pymysql.err.OperationalError as e:
                if e.args[0] != 1142:
                    raise
    finally:
        conn.close()
    return len(tables), total


def main() -> int:
    ap = argparse.ArgumentParser(description="安装 sakila 示例库并建只读账号")
    ap.add_argument("--force", action="store_true",
                    help="已存在 sakila 库时仍继续（schema 文件含 DROP SCHEMA，会清空重建）")
    args = ap.parse_args()

    mysql_exe = find_mysql_client()
    print(f"[0/5] mysql 客户端：{mysql_exe}")
    ensure_sql_files()

    root_pwd = getpass.getpass("请输入 MySQL root 密码（输入不回显）: ")

    probe = pymysql.connect(host=HOST, port=PORT, user="root", password=root_pwd)
    try:
        with probe.cursor() as cur:
            cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
                        (DB_NAME,))
            exists = cur.fetchone() is not None
    finally:
        probe.close()
    if exists and not args.force:
        print(f"✗ 库 {DB_NAME} 已存在。schema 文件开头是 DROP SCHEMA，继续会清空重建。")
        print("  确认无自有数据后重跑并加 --force；或换一个库名手工安装。")
        return 2
    print(f"[2/5] {DB_NAME} {'已存在，--force 覆盖重建' if exists else '不存在，全新安装'}")

    print(f"[3/5] 载入 schema（{SCHEMA_SQL.name}）…")
    load_via_cli(mysql_exe, root_pwd, SCHEMA_SQL, "schema")
    print(f"      载入 data（{DATA_SQL.name}，约 3.3 MB）…")
    load_via_cli(mysql_exe, root_pwd, DATA_SQL, "data")

    print(f"[4/5] 创建只读账号 {RO_USER}@localhost（仅 SELECT ON {DB_NAME}.*）…")
    ro_pwd = create_readonly_account(root_pwd)
    ENV_OUT.parent.mkdir(exist_ok=True)
    ENV_OUT.write_text(
        f"host={HOST}\nport={PORT}\nuser={RO_USER}\npassword={ro_pwd}\ndatabase={DB_NAME}\n",
        encoding="utf-8",
    )
    print(f"      凭据写入 {ENV_OUT}（已被 .gitignore 排除）")

    n_tables, n_rows = verify_readonly(ro_pwd)
    print(f"[5/5] ✓ 只读账号复核通过：{n_tables} 张基表 / {n_rows:,} 行；DELETE 被拒（errno 1142）")
    print("\n下一步：uv run python scripts/onboard.py --db-env config/db_sakila.env --dataset sakila")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
