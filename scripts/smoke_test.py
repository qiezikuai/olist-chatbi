"""早期链路冒烟脚本（历史排障用）：vanna 0.7.9 + MySQL(ecommerce/chatbi_ro) + SiliconFlow(DeepSeek)

前置（手工完成以下两项配置）：
  1) uv run python scripts/create_readonly_user.py   # 终端交互输 root 密码
  2) 复制 .env.example 为 .env，填入 SILICONFLOW_API_KEY

运行：uv run python scripts/smoke_test.py
通过标志：打印 1 条可执行 SQL + 查询结果

注意：本脚本含最小训练（1 条 DDL + 1 组问答），且样例为早期简版口径、未做干净重建——
请勿在正式库上运行；正式训练一律用 scripts/train.py（干净全量重建）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> None:
    key = load_env_file(ROOT / ".env").get("SILICONFLOW_API_KEY", "")
    if not key or key == "your_key_here":
        sys.exit("未找到 Key：请先复制 .env.example 为 .env 并填入 SILICONFLOW_API_KEY")

    db = load_env_file(ROOT / "config" / "db_ro.env")
    if not db:
        sys.exit("未找到 config/db_ro.env：请先运行 scripts/create_readonly_user.py")

    from openai import OpenAI
    from vanna.openai import OpenAI_Chat
    from vanna.chromadb import ChromaDB_VectorStore

    # vanna 0.7.9 的 OpenAI_Chat 只透传 config["api_key"]、忽略 base_url（读源码确认），
    # 必须自行构造带 base_url 的 client 注入，否则请求会发到 api.openai.com 导致超时
    llm_client = OpenAI(
        api_key=key,
        base_url="https://api.siliconflow.cn/v1",
        timeout=90,
        max_retries=2,
    )

    class MyVanna(ChromaDB_VectorStore, OpenAI_Chat):
        def __init__(self, client=None, config=None):
            ChromaDB_VectorStore.__init__(self, config=config)
            OpenAI_Chat.__init__(self, client=client, config=config)

    vn = MyVanna(
        client=llm_client,
        config={
            "model": "deepseek-ai/DeepSeek-V3.2",
            "path": str(ROOT / "chroma"),
            "language": "中文",
        },
    )
    vn.connect_to_mysql(
        host=db["host"],
        dbname=db["database"],
        user=db["user"],
        password=db["password"],
        port=int(db["port"]),
    )
    print("MySQL 已连接（chatbi_ro，仅 SELECT）")

    vn.train(
        ddl="CREATE TABLE olist_orders (order_id TEXT PRIMARY KEY, customer_id TEXT, "
        "order_status TEXT, order_purchase_timestamp DATETIME)"
    )
    vn.train(question="总共有多少笔订单？", sql="SELECT COUNT(*) AS order_count FROM olist_orders")

    question = "总共有多少笔订单？"
    sql = vn.generate_sql(question=question)
    print("生成 SQL:", sql)
    df = vn.run_sql(sql)
    print(df)
    print("== 冒烟通过 ==")


if __name__ == "__main__":
    main()
