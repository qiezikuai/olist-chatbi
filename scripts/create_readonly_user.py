"""Day1 P0.2：创建 chatbi_ro 只读账号（仅 SELECT @ ecommerce.*）

用法（发起人在自己的终端执行，root 密码交互输入，不经聊天、不落日志）：
    cd D:\\projects\\chatbi
    uv run python scripts/create_readonly_user.py

行为：随机生成 20 位密码 → 创建/重置 chatbi_ro@localhost → 仅授予
ecommerce.* 的 SELECT → 凭据写入 config/db_ro.env（已被 .gitignore 排除）。
"""
import secrets
import string
import getpass
from pathlib import Path

import pymysql


def main() -> None:
    root_pwd = getpass.getpass("请输入 MySQL root 密码（输入不回显）: ")
    conn = pymysql.connect(
        host="127.0.0.1", port=3306, user="root", password=root_pwd
    )
    alphabet = string.ascii_letters + string.digits
    ro_pwd = "".join(secrets.choice(alphabet) for _ in range(20))

    with conn.cursor() as cur:
        cur.execute(
            "CREATE USER IF NOT EXISTS 'chatbi_ro'@'localhost' IDENTIFIED BY %s",
            (ro_pwd,),
        )
        # 账号可能已存在（重跑场景）：重置密码并清掉旧授权
        cur.execute(
            "ALTER USER 'chatbi_ro'@'localhost' IDENTIFIED BY %s", (ro_pwd,)
        )
        try:
            cur.execute("REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'chatbi_ro'@'localhost'")
        except pymysql.err.OperationalError:
            pass  # 无历史授权时报错属预期
        cur.execute("GRANT SELECT ON ecommerce.* TO 'chatbi_ro'@'localhost'")
        cur.execute("FLUSH PRIVILEGES")
    conn.commit()
    conn.close()

    path = Path("config/db_ro.env")
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        "host=127.0.0.1\n"
        "port=3306\n"
        "user=chatbi_ro\n"
        f"password={ro_pwd}\n"
        "database=ecommerce\n",
        encoding="utf-8",
    )
    print("完成：chatbi_ro 已就绪（仅 SELECT ecommerce.*），凭据写入 config/db_ro.env（不入库）")


if __name__ == "__main__":
    main()
