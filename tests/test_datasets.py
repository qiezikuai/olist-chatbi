"""数据集解析层单测：路径解析、DDL 围栏提取、Olist 材料等价性、守卫规则按数据集隔离。"""
import pytest

from chatbi import knowledge
from chatbi.datasets import (DEFAULT_DATASET, Dataset, Materials, list_datasets,
                             load_materials, parse_ddl_fences, resolve)
from chatbi.guards import METRIC_RULES, check_caliber


# ---------- resolve：内置 olist ----------
def test_resolve_olist_uses_legacy_paths():
    ds = resolve("olist")
    assert ds.name == "olist"
    assert ds.materials_dir is None                       # 材料来自 knowledge 常量
    assert ds.chroma_dir.name == "chroma"                 # 既有向量库位置不变
    assert ds.db_env_path.name == "db_ro.env"             # 既有凭据文件不变
    assert ds.caliber_rules == tuple(METRIC_RULES)        # 已确认口径规则仍生效


def test_resolve_default_is_olist():
    assert resolve() == resolve(DEFAULT_DATASET)


def test_resolve_unknown_dataset_lists_available():
    with pytest.raises(RuntimeError) as e:
        resolve("no_such_dataset")
    msg = str(e.value)
    assert "no_such_dataset" in msg and "olist" in msg and "onboard.py" in msg


def test_list_datasets_always_contains_olist():
    assert DEFAULT_DATASET in list_datasets()


# ---------- DDL 围栏提取 ----------
FENCED = """# 某库表结构

## 表关系（外键）

```
film_actor.actor_id -> actor.actor_id
```

## actor（200 行）

```sql
CREATE TABLE `actor` (
  `actor_id` smallint unsigned NOT NULL,
  `first_name` varchar(45) NOT NULL,
  PRIMARY KEY (`actor_id`)
) ENGINE=InnoDB
```

## film（1000 行）

```sql
CREATE TABLE `film` (
  `film_id` smallint unsigned NOT NULL,
  PRIMARY KEY (`film_id`)
) ENGINE=InnoDB;
```
"""


def test_parse_ddl_fences_keeps_full_column_definitions():
    """取整块而非取"以 CREATE TABLE 开头的行"——后者会丢掉全部列定义。"""
    ddls = parse_ddl_fences(FENCED)
    assert len(ddls) == 2
    assert "`first_name` varchar(45) NOT NULL" in ddls[0]
    assert "PRIMARY KEY" in ddls[0]


def test_parse_ddl_fences_ignores_plain_fence_and_strips_semicolon():
    ddls = parse_ddl_fences(FENCED)
    assert not any("->" in d for d in ddls)          # 无语言标注的围栏不当 DDL
    assert not any(d.endswith(";") for d in ddls)    # 尾分号被去掉


def test_parse_ddl_fences_empty_when_no_sql_fence():
    assert parse_ddl_fences("# 只有标题\n\n```\nnot sql\n```\n") == []


# ---------- Olist 材料等价性（多数据集改造不得改变既有行为） ----------
def test_olist_materials_equal_legacy_constants():
    m = load_materials(resolve("olist"))
    assert m.ddl == knowledge.load_ddl()
    assert m.documentation == (knowledge.METRICS_CONTEXT.strip() + "\n" + knowledge.EXTRA_DOC.strip())
    assert m.qa_pairs == list(knowledge.QA_PAIRS) + list(knowledge.EXTRA_QA)
    assert len(m.qa_pairs) == 12


def test_materials_is_plain_container():
    m = Materials(ddl=["CREATE TABLE t (a INT)"], documentation="doc")
    assert m.qa_pairs == []


# ---------- 口径守卫按数据集隔离 ----------
def test_caliber_rules_empty_means_no_violations():
    """新数据集无已确认口径：不得拿 Olist 规则去强制重写它的 SQL。"""
    sql = "SELECT COUNT(*) FROM customer"
    assert check_caliber("有多少个客户", sql, ()) == []
    assert check_caliber("总 GMV 是多少", "SELECT SUM(amount) FROM payment", ()) == []


def test_caliber_rules_default_still_olist():
    """不传 rules 时保持原行为（Olist 规则），既有调用方与测试不受影响。"""
    assert check_caliber("有多少个客户", "SELECT COUNT(*) FROM customer") != []
    assert check_caliber("有多少个客户",
                         "SELECT COUNT(DISTINCT customer_unique_id) FROM olist_customers") == []


def test_dataset_caliber_rules_are_isolated(tmp_path):
    """非内置数据集解析出来规则为空——口径需人工确认后才可能成为规则。"""
    ds = Dataset(name="x", label="x", db_env_path=tmp_path / "db_x.env",
                 chroma_dir=tmp_path / "chroma")
    assert ds.caliber_rules == ()
