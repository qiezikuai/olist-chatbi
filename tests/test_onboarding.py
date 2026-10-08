"""数据源接入（onboarding）单测：只测纯函数与落盘产物，不连库、不调 LLM。"""
import pytest
import yaml

from chatbi import onboarding as ob_module
from chatbi.datasets import parse_ddl_fences
from chatbi.onboarding import (AI_TELL_WORDS, SchemaInfo, TableInfo, fk_lines, forbidden_words,
                               normalize_dataset_name, scan_forbidden, schema_brief,
                               write_metrics_md, write_qa_yaml, write_report, write_schema_md)

DDL_ACTOR = ("CREATE TABLE `actor` (\n"
             "  `actor_id` smallint unsigned NOT NULL AUTO_INCREMENT,\n"
             "  `first_name` varchar(45) NOT NULL,\n"
             "  PRIMARY KEY (`actor_id`)\n"
             ") ENGINE=InnoDB")
DDL_FILM = ("CREATE TABLE `film` (\n"
            "  `film_id` smallint unsigned NOT NULL,\n"
            "  `rental_rate` decimal(4,2) NOT NULL,\n"
            "  PRIMARY KEY (`film_id`)\n"
            ") ENGINE=InnoDB")


@pytest.fixture
def info():
    return SchemaInfo(
        database="fakedb",
        tables=[
            TableInfo(name="actor", rows=200, rows_exact=True, ddl=DDL_ACTOR, comment="演员表",
                      columns=[("actor_id", "smallint unsigned", "NO", "PRI", ""),
                               ("first_name", "varchar(45)", "NO", "", "名字")]),
            TableInfo(name="film", rows=1000, rows_exact=False, ddl=DDL_FILM, comment="",
                      columns=[("film_id", "smallint unsigned", "NO", "PRI", ""),
                               ("rental_rate", "decimal(4,2)", "NO", "", "")]),
        ],
        fks=[{"table": "film_actor", "column": "actor_id",
              "ref_table": "actor", "ref_column": "actor_id"}],
        views=["film_list"],
    )


@pytest.fixture
def qa_good():
    return [{"question": "一共有多少位演员？", "sql": "SELECT COUNT(*) AS n FROM actor",
             "type": "单表聚合", "verified": True, "rows": 1}]


# ---------- 数据集名校验（会拼进文件路径，必须防穿越） ----------
@pytest.mark.parametrize("bad", ["../evil", "..", "a/b", "C:\\x", "", "a" * 40, "-x", "_x", "a b"])
def test_normalize_dataset_name_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        normalize_dataset_name(bad)


@pytest.mark.parametrize("good,expect", [("sakila", "sakila"), ("My_DB-2", "my_db-2"),
                                         ("  employees  ", "employees"), ("UPPER", "upper")])
def test_normalize_dataset_name_accepts_and_lowercases(good, expect):
    assert normalize_dataset_name(good) == expect


# ---------- schema.md 落盘与 DDL 往返 ----------
def test_write_schema_md_roundtrip_keeps_full_ddl(tmp_path, info):
    p = write_schema_md(tmp_path, info)
    ddls = parse_ddl_fences(p.read_text(encoding="utf-8"))
    assert ddls == [DDL_ACTOR, DDL_FILM]        # 完整 DDL，含列定义
    assert "`first_name` varchar(45) NOT NULL" in ddls[0]


def test_write_schema_md_records_facts(tmp_path, info):
    text = write_schema_md(tmp_path, info).read_text(encoding="utf-8")
    assert "2 张基表" in text and "1,200 行" in text
    assert "film_actor.actor_id -> actor.actor_id" in text
    assert "1 个视图" in text
    assert "（估算）" in text                     # COUNT 超时的表如实标注为估算


def test_write_schema_md_without_fk_warns_human(tmp_path):
    info = SchemaInfo(database="d", tables=[TableInfo("t", 1, True, "CREATE TABLE `t` (a INT)")])
    text = write_schema_md(tmp_path, info).read_text(encoding="utf-8")
    assert "未声明外键约束" in text
    assert parse_ddl_fences(text) == ["CREATE TABLE `t` (a INT)"]


def test_fk_lines(info):
    assert fk_lines(info) == ["film_actor.actor_id -> actor.actor_id"]


# ---------- 给 LLM 的表结构摘要 ----------
def test_schema_brief_contains_tables_columns_and_fks(info):
    b = schema_brief(info)
    assert "表 actor（200 行）：演员表" in b
    assert "first_name varchar(45) NOT NULL" in b
    assert "actor_id smallint unsigned NOT NULL PK" in b
    assert "外键关系" in b and "film_actor.actor_id -> actor.actor_id" in b
    assert "估算" in b                            # film 行数为估算，须让 LLM 知道


def test_schema_brief_truncates_when_too_long(info, monkeypatch):
    import chatbi.onboarding as ob
    monkeypatch.setattr(ob, "MAX_BRIEF_CHARS", 50)
    assert schema_brief(info).endswith("（表结构过长，此处已截断）")


def test_schema_brief_never_contains_credentials(info):
    b = schema_brief(info)
    assert "password" not in b.lower()


# ---------- 禁词扫描：生成材料会入库，不得带 AI 套话或内部用语 ----------
def test_scan_forbidden_passes_clean_text():
    scan_forbidden("# 口径\n\nGMV = SUM(price)", "metrics.md")     # 不抛即通过


@pytest.mark.parametrize("word", AI_TELL_WORDS)
def test_scan_forbidden_blocks_ai_tells(word):
    with pytest.raises(RuntimeError) as e:
        scan_forbidden(f"正文里混进了「{word}」这类套话", "metrics.md")
    assert word in str(e.value)


def test_forbidden_words_includes_local_list(tmp_path, monkeypatch):
    """项目内部用语清单来自 gitignore 的本地文件——清单本身不随代码入库。"""
    import chatbi.onboarding as ob
    local = tmp_path / "forbidden_words.txt"
    local.write_text("# 注释行应被忽略\n内部用语甲\n\n内部用语乙\n", encoding="utf-8")
    monkeypatch.setattr(ob, "LOCAL_FORBIDDEN_FILE", local)
    words = forbidden_words()
    assert "内部用语甲" in words and "内部用语乙" in words
    assert "# 注释行应被忽略" not in words and "注释行应被忽略" not in words
    assert set(AI_TELL_WORDS) <= set(words)          # 通用清单始终生效
    with pytest.raises(RuntimeError):
        scan_forbidden("这段候选口径提到了内部用语甲", "metrics.md")


def test_forbidden_words_without_local_file(tmp_path, monkeypatch):
    import chatbi.onboarding as ob
    monkeypatch.setattr(ob, "LOCAL_FORBIDDEN_FILE", tmp_path / "absent.txt")
    assert forbidden_words() == AI_TELL_WORDS


def test_committed_modules_carry_no_internal_vocabulary():
    """禁词清单本身不入库，故用本地清单反查会提交的模块源码；清单缺失则跳过。

    本测试刻意不写死任何内部用语——写死就等于把它带进公开仓库。
    """
    import inspect

    import chatbi.datasets
    local_only = tuple(w for w in forbidden_words() if w not in AI_TELL_WORDS)
    if not local_only:
        pytest.skip("本地禁词清单不存在（config/forbidden_words.txt），跳过")
    for mod in (ob_module, chatbi.datasets):
        hits = [w for w in local_only if w in inspect.getsource(mod)]
        assert not hits, f"{mod.__name__} 里出现了不该入库的用词 {hits}"


def test_write_metrics_md_refuses_forbidden_words(tmp_path, info):
    with pytest.raises(RuntimeError):
        write_metrics_md(tmp_path, info, "作为一个AI，我给出以下口径")


# ---------- metrics.md：必须显式标注候选/待人工确认 ----------
def test_write_metrics_md_marks_candidate_and_separates_facts(tmp_path, info):
    text = write_metrics_md(tmp_path, info, "【事实】actor 主键为 actor_id\n【待确认】是否排除测试数据").read_text(encoding="utf-8")
    assert "候选 · 待人工确认" in text
    assert "口径是业务定义" in text
    assert "不会" in text and "强制规则" in text          # 说明未确认口径不生效
    assert "- film_actor.actor_id -> actor.actor_id" in text   # 外键是事实，单列一节
    assert "【待确认】是否排除测试数据" in text


# ---------- qa.yaml：只收录实跑验证通过的 ----------
def test_write_qa_yaml_roundtrip(tmp_path, info, qa_good):
    p = write_qa_yaml(tmp_path, "fakedb", info, qa_good)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["dataset"] == "fakedb" and data["database"] == "fakedb"
    assert data["qa_pairs"][0]["question"] == "一共有多少位演员？"
    assert data["qa_pairs"][0]["verified"] is True
    assert "实跑验证" in data["note"]


def test_write_qa_yaml_keeps_sql_verbatim(tmp_path, info, qa_good):
    p = write_qa_yaml(tmp_path, "fakedb", info, qa_good)
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["qa_pairs"][0]["sql"] == qa_good[0]["sql"]


# ---------- 接入报告 ----------
def test_write_report_states_automation_boundary(tmp_path, info, qa_good):
    trials = [{"question": qa_good[0]["question"], "ok": True, "reason": "",
               "gen_rows": 1, "ref_rows": 1, "gen_sql": qa_good[0]["sql"]}]
    text = write_report(tmp_path, "fakedb", info,
                        counts={"ddl": 2, "metrics_chars": 900, "qa_gen": 12,
                                "qa_good": 10, "qa_bad": 2},
                        bad=[{"question": "坏问题", "reason": "SEMANTIC: unknown column"}],
                        trials=trials, risky_grants=[]).read_text(encoding="utf-8")
    assert "自动化边界" in text
    assert "需人工确认" in text and "指标口径" in text
    assert "1/1 通过" in text
    assert "实跑验证通过 **10** 组" in text and "淘汰 2 组" in text
    assert "unknown column" in text                 # 淘汰项如实记录，不静默丢弃
    assert "仅 SELECT" in text


def test_write_report_flags_writable_account(tmp_path, info, qa_good):
    text = write_report(tmp_path, "fakedb", info,
                        counts={"ddl": 2, "metrics_chars": 1, "qa_gen": 1, "qa_good": 1, "qa_bad": 0},
                        bad=[], trials=[], risky_grants=["ALL PRIVILEGES"]).read_text(encoding="utf-8")
    assert "安全提示" in text and "ALL PRIVILEGES" in text and "最小权限只读账号" in text


def test_write_report_notes_trial_is_not_accuracy(tmp_path, info, qa_good):
    text = write_report(tmp_path, "fakedb", info,
                        counts={"ddl": 2, "metrics_chars": 1, "qa_gen": 1, "qa_good": 1, "qa_bad": 0},
                        bad=[], trials=[], risky_grants=[]).read_text(encoding="utf-8")
    assert "不等于准确率" in text
