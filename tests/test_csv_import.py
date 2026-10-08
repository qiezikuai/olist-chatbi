"""CSV 导入器单测：编码 / 分隔符 / 表头 / 类型推断 / 脏数据兜底。全部用临时小文件，不依赖外部数据。"""
import sqlite3

import pytest

from chatbi.csv_import import (clean_columns, coerce, detect_delimiter, detect_encoding,
                               has_header, import_csv, infer_types, read_table,
                               sanitize_table_name)


def write(tmp_path, name, text, encoding="utf-8"):
    p = tmp_path / name
    p.write_text(text, encoding=encoding, newline="")
    return p


# ---------- 编码 ----------
def test_detect_encoding_gbk(tmp_path):
    p = write(tmp_path, "gbk.csv", "书名,价格\n新编教程,32.00\n", encoding="gbk")
    assert detect_encoding(p) == "gbk"


def test_detect_encoding_utf8_bom(tmp_path):
    p = write(tmp_path, "bom.csv", "a,b\n1,2\n", encoding="utf-8-sig")
    assert detect_encoding(p) == "utf-8-sig"


def test_detect_encoding_plain_utf8(tmp_path):
    p = write(tmp_path, "u8.csv", "name,city\n张三,上海\n", encoding="utf-8")
    assert detect_encoding(p) == "utf-8"


# ---------- 分隔符 ----------
def test_detect_delimiter_semicolon():
    assert detect_delimiter(['"a";"b";"c"', '"1";"2";"3"']) == ";"


def test_detect_delimiter_comma_with_quoted_comma_inside():
    """引号内的逗号不得计入——否则含逗号的字段会骗过分隔符判定。"""
    lines = ['name,note', '"Allen, Mr. W",x', '"Brown, Mrs. B",y']
    assert detect_delimiter(lines) == ","


def test_detect_delimiter_tab():
    assert detect_delimiter(["a\tb\tc", "1\t2\t3"]) == "\t"


# ---------- 表头 ----------
def test_has_header_true_for_normal_csv():
    assert has_header([["id", "name"], ["1", "Alice"], ["2", "Bob"]]) is True


def test_has_header_false_for_all_numeric_first_row():
    """iris.data 型：首行与数据行同为全数值 → 无表头。"""
    rows = [["5.1", "3.5", "1.4", "setosa"], ["4.9", "3.0", "1.4", "setosa"],
            ["4.7", "3.2", "1.3", "setosa"]]
    assert has_header(rows) is False


def test_has_header_true_for_all_text_with_header():
    """全文本但有真表头：首行与数据行都非数值时默认按有表头处理（不硬猜）。"""
    assert has_header([["title", "author"], ["书A", "作者A"], ["书B", "作者B"]]) is True


# ---------- 列名清洗 ----------
def test_clean_columns_strips_bom_quotes_spaces_and_dedups():
    got = clean_columns(["\ufeff id", " name ", "name", "", "2nd"], 5)
    assert got[0] == "id"
    assert got[1] == "name"
    assert got[2] == "name_1"          # 重名加序号
    assert got[3] == "col_4"           # 空名合成
    assert got[4] == "c_2nd"           # 数字开头加前缀


def test_clean_columns_keeps_chinese():
    assert clean_columns(["书名", "价格"], 2) == ["书名", "价格"]


def test_clean_columns_pads_to_ncols():
    assert clean_columns(["a"], 3) == ["a", "col_2", "col_3"]


def test_sanitize_table_name():
    assert sanitize_table_name("olist_orders_dataset") == "olist_orders_dataset"
    assert sanitize_table_name("My File-1!") == "my_file_1"
    assert sanitize_table_name("123abc") == "t_123abc"


# ---------- 类型推断与转换 ----------
def test_infer_types_mixed():
    rows = [["1", "2.5", "x", ""], ["2", "3.5", "y", ""], ["3", "4.5", "z", ""]]
    assert infer_types(rows, 4) == ["INTEGER", "REAL", "TEXT", "TEXT"]


def test_infer_types_dirty_value_falls_back_to_text():
    rows = [["1"], ["2"], ["n/a 件"]]
    assert infer_types(rows, 1) == ["TEXT"]


@pytest.mark.parametrize("v,typ,expect", [
    ("", "INTEGER", None), ("NA", "INTEGER", None), ("null", "TEXT", None),
    ("7", "INTEGER", 7), ("7.5", "REAL", 7.5), ("abc", "INTEGER", "abc"),   # 脏值保留原文不丢行
    ("  x  ", "TEXT", "x"),
])
def test_coerce(v, typ, expect):
    assert coerce(v, typ) == expect


# ---------- 端到端导入 ----------
def test_import_csv_roundtrip_and_nulls(tmp_path):
    p = write(tmp_path, "t.csv", "id,name,score\n1,Alice,9.5\n2,,NA\n3,Bob,\n")
    r = import_csv(p, tmp_path / "t.db")
    assert r["row_count"] == 3 and r["header_detected"] is True
    assert dict(r["columns"]) == {"id": "INTEGER", "name": "TEXT", "score": "REAL"}
    conn = sqlite3.connect(r["db_path"])
    rows = conn.execute('SELECT id, name, score FROM "t" ORDER BY id').fetchall()
    assert rows == [(1, "Alice", 9.5), (2, None, None), (3, "Bob", None)]
    conn.close()


def test_import_csv_ragged_rows_padded_and_truncated(tmp_path):
    p = write(tmp_path, "r.csv", "a,b,c\n1,2\n1,2,3,4\n")
    r = import_csv(p, tmp_path / "r.db")
    conn = sqlite3.connect(r["db_path"])
    rows = conn.execute('SELECT a, b, c FROM "r"').fetchall()
    assert rows[0] == (1, 2, None)      # 短行补空 → NULL
    assert rows[1] == (1, 2, 3)         # 长行截断
    conn.close()


def test_import_csv_headerless_with_given_names(tmp_path):
    p = write(tmp_path, "h.csv", "1,x\n2,y\n")
    r = import_csv(p, tmp_path / "h.db", header_names=["num", "tag"])
    assert r["header_detected"] is False
    conn = sqlite3.connect(r["db_path"])
    assert conn.execute('SELECT num, tag FROM "h" ORDER BY num').fetchall() == [(1, "x"), (2, "y")]
    conn.close()


def test_import_csv_semicolon_chinese_crlf(tmp_path):
    p = write(tmp_path, "s.csv", "书名;价格;作者\n新编教程;32.00;王著\n庄子;13.00;庄周\r\n",
              encoding="utf-8")
    r = import_csv(p, tmp_path / "s.db")
    assert r["delimiter"] == ";" and r["row_count"] == 2
    conn = sqlite3.connect(r["db_path"])
    assert conn.execute('SELECT 价格 FROM "s" ORDER BY 价格').fetchall() == [(13.0,), (32.0,)]
    conn.close()


def test_read_table_reports_diagnostics(tmp_path):
    p = write(tmp_path, "d.csv", "a,b\n1,2\n")
    d = read_table(p)
    assert d["encoding"] == "utf-8" and d["delimiter"] == "," and d["header_detected"] is True


def test_import_csv_empty_raises(tmp_path):
    p = write(tmp_path, "e.csv", "\n\n")
    with pytest.raises(ValueError):
        import_csv(p, tmp_path / "e.db")


def test_import_csv_header_only_raises(tmp_path):
    p = write(tmp_path, "ho.csv", "a,b\n")
    with pytest.raises(ValueError):
        import_csv(p, tmp_path / "ho.db")
