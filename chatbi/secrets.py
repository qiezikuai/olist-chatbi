"""凭据/配置读取（单源）。

- read_llm_key()：从 .env 读 SiliconFlow API Key（python-dotenv 解析）。
- read_db_config()：从 config/db_ro.env 读只读账号连接配置。
两个文件均在 .gitignore 中：凭据绝不打印、绝不入 git、绝不进 AI 上下文。
"""
from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent

_ENV_FILE = ROOT / ".env"
_DB_FILE = ROOT / "config" / "db_ro.env"


def read_llm_key(env_path: str | Path | None = None) -> str:
    """返回 SILICONFLOW_API_KEY；未配置则抛 RuntimeError（不打印值）。"""
    p = Path(env_path) if env_path else _ENV_FILE
    if not p.exists():
        raise RuntimeError("未找到 .env：请复制 .env.example 为 .env 并填入 SILICONFLOW_API_KEY")
    key = (dotenv_values(p).get("SILICONFLOW_API_KEY") or "").strip()
    if not key or key == "your_key_here":
        raise RuntimeError("SILICONFLOW_API_KEY 未就绪（.env 未填或为占位符）")
    return key


def read_db_config(env_path: str | Path | None = None) -> dict[str, str]:
    """返回 {host, port, user, password, database}（chatbi_ro 只读账号）。"""
    p = Path(env_path) if env_path else _DB_FILE
    if not p.exists():
        raise RuntimeError("未找到 config/db_ro.env：请先运行 uv run python scripts/create_readonly_user.py")
    cfg = {k: (v or "").strip() for k, v in dotenv_values(p).items() if v is not None}
    missing = {"host", "user", "password", "database"} - cfg.keys()
    if missing:
        raise RuntimeError(f"config/db_ro.env 缺字段：{sorted(missing)}")
    cfg.setdefault("port", "3306")
    return cfg
