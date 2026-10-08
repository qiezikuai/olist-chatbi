"""SQLite 执行闸单测：与 MySQL 同构的四道防线 + 只读打开 + Python 层超时。"""
import sqlite3
import time

import pytest

from chatbi.datasets import Dataset
from chatbi.executor import SqliteExecutor, executor_for_dataset


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    conn.execute('CREATE TABLE "t" ("a" INTEGER, "b" TEXT)')
    conn.executemany('INSERT INTO "t" VALUES (?, ?)', [(i, f"v{i}") for i in range(50)])
    conn.commit()
    conn.close()
    return p


def ex(db, **kw):
    return SqliteExecutor(path=db, **kw)


# ---------- 正常执行 ----------
def test_select_returns_rows_and_columns(db):
    r = ex(db).execute('SELECT a, b FROM "t" WHERE a < 3 ORDER BY a')
    assert r.ok and r.row_count == 3 and r.columns == ["a", "b"]
    assert r.rows[0] == (0, "v0")


def test_limit_injected_when_absent(db):
    r = ex(db, max_rows=7).execute('SELECT a FROM "t"')
    assert r.ok and r.row_count == 7 and r.sql.rstrip().endswith("LIMIT 7")


def test_limit_not_duplicated(db):
    r = ex(db, max_rows=7).execute('SELECT a FROM "t" LIMIT 3')
    assert r.ok and r.row_count == 3 and r.sql.upper().count("LIMIT") == 1


# ---------- 防线 1：白名单（与 MySQL 共用，方言无关） ----------
@pytest.mark.parametrize("sql", [
    'DELETE FROM "t"', 'UPDATE "t" SET a=1', 'DROP TABLE "t"',
    'SELECT 1; DROP TABLE "t"', "ATTACH DATABASE 'x.db' AS x",
])
def test_write_and_multi_statement_blocked(db, sql):
    r = ex(db).execute(sql)
    assert not r.ok and r.error.code == "BLOCKED" and r.error.stage == "whitelist"


def test_blocked_before_touching_db(db):
    """白名单拒绝不触库：拒绝后表数据不变。"""
    ex(db).execute('DELETE FROM "t"')
    conn = sqlite3.connect(db)
    assert conn.execute('SELECT COUNT(*) FROM "t"').fetchone()[0] == 50
    conn.close()


# ---------- 只读打开：第二道闸 ----------
def test_db_opened_read_only(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError) as e:
        conn.execute('DELETE FROM "t" WHERE a = 0')
    assert "readonly" in str(e.value).lower() or "read-only" in str(e.value).lower()
    conn.close()


def test_normalize_readonly_as_permission(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.execute('DELETE FROM "t"')
    except sqlite3.Error as e:
        err = ex(db)._normalize(e, stage="execute")
    finally:
        conn.close()
    assert err.code == "PERMISSION"


# ---------- 错误归一化（与 MySQL 同构的 code） ----------
def test_unknown_column_is_semantic(db):
    r = ex(db).execute('SELECT no_such_col FROM "t"')
    assert not r.ok and r.error.code == "SEMANTIC"


def test_unknown_table_is_semantic(db):
    r = ex(db).execute('SELECT * FROM no_such_table')
    assert not r.ok and r.error.code == "SEMANTIC"


def test_syntax_error(db):
    r = ex(db).execute('SELECT FROM WHERE')
    assert not r.ok and r.error.code == "SYNTAX"


# ---------- 超时：set_progress_handler ----------
def test_progress_handler_fires_when_deadline_passed(db):
    e = ex(db, timeout_s=5)
    e._deadline = time.perf_counter() - 1
    assert e._progress_hit() == 1
    e._deadline = time.perf_counter() + 60
    assert e._progress_hit() == 0


def test_slow_query_interrupted_as_timeout(db):
    r = ex(db, timeout_s=1).execute(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<30000000) "
        "SELECT COUNT(*) FROM c")
    assert not r.ok and r.error.code == "TIMEOUT"


# ---------- 生命周期 ----------
def test_connection_reused_and_close(db):
    e = ex(db)
    e.execute('SELECT 1 FROM "t" LIMIT 1')
    first = e._conn
    e.execute('SELECT 1 FROM "t" LIMIT 1')
    assert e._conn is first
    e.close()
    assert e._conn is None


def test_context_manager_closes(db):
    with ex(db) as e:
        assert e.execute('SELECT 1 FROM "t" LIMIT 1').ok
    assert e._conn is None


# ---------- 工厂 ----------
def test_factory_builds_sqlite_executor(db):
    ds = Dataset(name="x", label="x", chroma_dir=db.parent / "chroma",
                 backend="sqlite", sqlite_path=db)
    e = executor_for_dataset(ds)
    assert isinstance(e, SqliteExecutor)
    assert e.execute('SELECT COUNT(*) FROM "t"').ok
    e.close()


def test_factory_rejects_missing_sqlite_file(tmp_path):
    ds = Dataset(name="x", label="x", chroma_dir=tmp_path / "chroma",
                 backend="sqlite", sqlite_path=tmp_path / "absent.db")
    with pytest.raises(RuntimeError) as e:
        executor_for_dataset(ds)
    assert "sqlite" in str(e.value) and "onboard.py" in str(e.value)
