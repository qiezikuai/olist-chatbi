"""数据集解析：一套训练材料 + 一个只读库 + 一个独立向量库 + 该库适用的口径规则。

内置 `olist` 数据集沿用既有路径与 `chatbi/knowledge.py` 的模块常量，行为与引入
多数据集之前完全一致（默认值，未做迁移）。

其他数据集为约定式目录 `datasets/<name>/`，材料由 `scripts/onboard.py` 生成：

    schema.md   表结构（每表一个 ```sql 围栏，含行数）——完整 DDL，非仅表名行
    metrics.md  指标口径候选（自动生成，**需人工确认后**才算业务口径）
    qa.yaml     问答对（question / sql / type / verified）
    chroma/     该数据集独立向量库（运行产物，已 gitignore）
    dataset.yaml  后端清单（backend: mysql|sqlite、sqlite 库文件路径、label）

两种后端：
    mysql   连接凭据在 `config/db_<name>.env`（已 gitignore），由接入时创建的最小权限只读账号提供
    sqlite  库文件在 `datasets/<name>/data.db`（已 gitignore），由 CSV 导入生成；
            执行器以 mode=ro 只读打开，无凭据概念

口径守卫规则按数据集隔离：只有 olist 携带已人工确认的规则（见 chatbi/guards.py）。
新数据集默认为空——口径是业务定义，自动生成的候选未经确认，拿它当规则去强制
重写 SQL 会把本来正确的查询改错（本项目评估期踩过一次同类坑）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from chatbi.guards import METRIC_RULES

ROOT = Path(__file__).resolve().parent.parent
DATASETS_DIR = ROOT / "datasets"
CONFIG_DIR = ROOT / "config"

DEFAULT_DATASET = "olist"


@dataclass(frozen=True)
class Materials:
    """三路训练材料。knowledge.build() 只认这个结构，不认材料来自哪里。"""
    ddl: list[str]                  # 第 1 路：建表语句
    documentation: str              # 第 2 路：指标口径文档
    qa_pairs: list[dict] = field(default_factory=list)   # 第 3 路：{question, sql}


@dataclass(frozen=True)
class Dataset:
    name: str
    label: str
    chroma_dir: Path                # 独立向量库目录
    backend: str = "mysql"          # mysql | sqlite
    db_env_path: Path | None = None     # mysql：只读账号连接信息（gitignore）
    sqlite_path: Path | None = None     # sqlite：库文件路径（无凭据）
    caliber_rules: tuple = ()       # 口径守卫规则；空 = 该库暂无已确认口径
    materials_dir: Path | None = None   # None = 材料来自 chatbi/knowledge.py 常量（olist）


def resolve(name: str = DEFAULT_DATASET) -> Dataset:
    """按名解析数据集。未知名字给出可用清单，不静默回退到默认库。"""
    if name == DEFAULT_DATASET:
        return Dataset(
            name="olist",
            label="Olist 电商数仓（ecommerce，9 表 / 155 万行）",
            chroma_dir=ROOT / "chroma",
            backend="mysql",
            db_env_path=CONFIG_DIR / "db_ro.env",
            caliber_rules=tuple(METRIC_RULES),
            materials_dir=None,
        )

    d = DATASETS_DIR / name
    manifest = d / "dataset.yaml"
    meta: dict = {}
    if manifest.exists():
        meta = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    backend = str(meta.get("backend", "mysql")).strip().lower()
    label = str(meta.get("label") or name)

    if not d.is_dir() or not (d / "schema.md").exists():
        raise RuntimeError(
            f"未找到数据集 '{name}'（期望 {d / 'schema.md'}）。"
            f"可用数据集：{', '.join(list_datasets())}；"
            f"接入新库：uv run python scripts/onboard.py"
        )

    if backend == "sqlite":
        rel = str(meta.get("sqlite_path") or "data.db")
        sp = Path(rel) if Path(rel).is_absolute() else d / rel
        if not sp.exists():
            raise RuntimeError(f"数据集 '{name}' 声明 sqlite 后端但库文件不存在：{sp}；"
                               f"请重跑 scripts/onboard.py --csv")
        return Dataset(name=name, label=label, chroma_dir=d / "chroma", backend="sqlite",
                       db_env_path=None, sqlite_path=sp, caliber_rules=(), materials_dir=d)

    env_path = CONFIG_DIR / f"db_{name}.env"
    if not env_path.exists():
        raise RuntimeError(
            f"数据集 '{name}' 缺连接凭据 {env_path}：请先建最小权限只读账号并写入该文件"
        )
    label_file = d / "dataset.txt"
    if not meta.get("label") and label_file.exists():
        label = label_file.read_text(encoding="utf-8").strip() or name
    return Dataset(
        name=name,
        label=label,
        chroma_dir=d / "chroma",
        backend="mysql",
        db_env_path=env_path,
        caliber_rules=(),          # 未经人工确认的口径不作为强制规则，见模块注释
        materials_dir=d,
    )


def list_datasets() -> list[str]:
    """可解析的数据集名（内置 olist + datasets/ 下材料齐备的目录）。"""
    names = [DEFAULT_DATASET]
    if DATASETS_DIR.is_dir():
        names += sorted(p.name for p in DATASETS_DIR.iterdir()
                        if p.is_dir() and (p / "schema.md").exists())
    return names


def parse_ddl_fences(markdown: str) -> list[str]:
    """从 markdown 的 ```sql 围栏中提取完整 DDL（一表一块）。

    取整块而非取"以 CREATE TABLE 开头的行"：后者只会拿到表名行、丢掉全部列定义。
    """
    blocks = re.findall(r"```sql\s*\n(.*?)```", markdown, re.S)
    return [b.strip().rstrip(";").strip() for b in blocks if b.strip()]


def load_materials(ds: Dataset) -> Materials:
    """加载该数据集的三路材料。olist 走 chatbi/knowledge.py 的既有常量。"""
    if ds.materials_dir is None:
        # 延迟 import：knowledge 在本模块类型上依赖 Materials，避免模块级环
        from chatbi import knowledge
        return Materials(
            ddl=knowledge.load_ddl(),
            documentation=(knowledge.METRICS_CONTEXT.strip() + "\n" + knowledge.EXTRA_DOC.strip()),
            qa_pairs=list(knowledge.QA_PAIRS) + list(knowledge.EXTRA_QA),
        )

    d = ds.materials_dir
    schema_md = (d / "schema.md").read_text(encoding="utf-8")
    ddl = parse_ddl_fences(schema_md)
    if not ddl:
        raise RuntimeError(f"{d / 'schema.md'} 里没有解析到 ```sql 围栏，无法作为 DDL 训练材料")

    metrics_path = d / "metrics.md"
    documentation = metrics_path.read_text(encoding="utf-8").strip() if metrics_path.exists() else ""

    qa_path = d / "qa.yaml"
    qa_pairs: list[dict] = []
    if qa_path.exists():
        data = yaml.safe_load(qa_path.read_text(encoding="utf-8")) or {}
        raw = data.get("qa_pairs", data) if isinstance(data, dict) else data
        for item in raw or []:
            if isinstance(item, dict) and item.get("question") and item.get("sql"):
                qa_pairs.append({"question": str(item["question"]), "sql": str(item["sql"])})

    return Materials(ddl=ddl, documentation=documentation, qa_pairs=qa_pairs)
