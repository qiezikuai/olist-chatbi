"""CSV → SQLite 导入器：把"给一个数据文件"变成"给一个库"。

真实 CSV 的常见脏处与对应兜底：
  - 编码：utf-8-sig → utf-8 → gbk → gb18030 依次整文件试解码，首个全通过者命中；
    都不行退 latin-1（逐字节映射，宁可个别乱码也不丢行）。
  - 分隔符：逗号 / 分号 / 制表符 / 竖线，按"引号外计数、各行是否一致"打分选最优。
  - 表头：只在"首行与数据行同为全数值"时判为无表头（如 iris.data）；其余默认有表头。
    **全文本的无表头文件在统计上与有表头文件不可区分**，这种情况请用 header_names
    显式给列名——猜错列名比承认需要人工更糟。
  - 列名：去 BOM/空白/包裹引号，非法字符转下划线，重名加序号，空名合成 col_N；
    保留中文列名（建表时标识符一律加引号，SQLite 支持）。
  - 类型：按列抽样推断 INTEGER / REAL / TEXT；空串与常见空值记号（NA/null/…）转 NULL；
    个别与列类型不符的脏值保留原文（SQLite 动态类型），**不丢行**。
  - 行宽：短行补空、长行截断（脏 CSV 常见）。
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from pathlib import Path

_ENCODINGS = ("utf-8", "gbk", "gb18030")   # utf-8-sig 不走这里：见 detect_encoding 的 BOM 检查
_DELIMITERS = (",", ";", "\t", "|")
_NULLS = {"", "na", "n/a", "null", "none", "nan", "-", "--", "?"}
_INT_RE = re.compile(r"^[+-]?\d+$")
_REAL_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def detect_encoding(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"      # 真有 BOM 才报 sig；否则 utf-8-sig 会把无 BOM 文件也吞掉
    for enc in _ENCODINGS:
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def _count_outside_quotes(line: str, ch: str) -> int:
    n, in_q = 0, False
    for c in line:
        if c == '"':
            in_q = not in_q
        elif c == ch and not in_q:
            n += 1
    return n


def detect_delimiter(lines: list) -> str:
    """引号外计数最多、且各行计数一致的候选胜出（一致 = 更像真分隔符）。"""
    best, best_score = ",", (-1, -1.0)
    for d in _DELIMITERS:
        counts = [_count_outside_quotes(l, d) for l in lines[:20] if l.strip()]
        nonzero = [c for c in counts if c > 0]
        if not nonzero:
            continue
        consistent = 1 if len(set(nonzero)) == 1 else 0
        score = (consistent, sum(nonzero) / len(nonzero))
        if score > best_score:
            best, best_score = d, score
    return best


def _looks_numeric(v: str) -> bool:
    return bool(_INT_RE.match(v.strip()) or _REAL_RE.match(v.strip()))


def has_header(rows: list) -> bool:
    """按列看数值信号：某列数据行多为数值而首行该列不是数值 → 首行是表头。

    - iris.data 型（无表头、含文本列）：数值列的首行同样是数值 → 判无表头。
    - 全文本文件没有任何数值信号，统计上不可区分 → 按有表头处理（同 pandas 默认），
      真无表头的全文本文件请用 header_names 显式给列名。
    """
    if len(rows) < 2:
        return True
    first = rows[0]
    if not any(c.strip() for c in first):
        return False
    data = [r for r in rows[1:21] if any(c.strip() for c in r)]
    if not data:
        return True
    ncols = min(len(first), max(len(r) for r in data))
    numeric_cols = mismatch = 0
    for c in range(ncols):
        col_vals = [r[c] for r in data if c < len(r) and r[c].strip()]
        if not col_vals:
            continue
        if sum(1 for v in col_vals if _looks_numeric(v)) / len(col_vals) < 0.8:
            continue
        numeric_cols += 1
        if not _looks_numeric(first[c] if c < len(first) else ""):
            mismatch += 1
    if numeric_cols == 0:
        return True
    return mismatch > 0


def clean_columns(header: list, ncols: int) -> list:
    seen: dict = {}
    out: list = []
    for i, raw in enumerate(header):
        name = (raw or "").replace("\ufeff", "").strip().strip("\"'").strip()
        name = re.sub(r"[^0-9A-Za-z_一-鿿]+", "_", name).strip("_")
        if name and name[0].isdigit():
            name = "c_" + name
        if not name:
            name = f"col_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    while len(out) < ncols:
        out.append(f"col_{len(out) + 1}")
    return out[:ncols]


def _is_null(v: str) -> bool:
    return v.strip().lower() in _NULLS


def infer_types(data_rows: list, ncols: int, sample: int = 2000) -> list:
    types = []
    for c in range(ncols):
        vals = [r[c] for r in data_rows[:sample] if c < len(r) and not _is_null(r[c])]
        if not vals:
            types.append("TEXT")
        elif all(_INT_RE.match(v.strip()) for v in vals):
            types.append("INTEGER")
        elif all(_REAL_RE.match(v.strip()) for v in vals):
            types.append("REAL")
        else:
            types.append("TEXT")
    return types


def coerce(v: str, typ: str):
    """按推断类型转换；空值转 NULL；与列类型不符的脏值保留原文（不丢行）。"""
    if _is_null(v):
        return None
    s = v.strip()
    if typ == "INTEGER":
        try:
            return int(s)
        except ValueError:
            return s
    if typ == "REAL":
        try:
            return float(s)
        except ValueError:
            return s
    return s


def sanitize_table_name(stem: str) -> str:
    name = re.sub(r"[^0-9A-Za-z_一-鿿]+", "_", stem).strip("_").lower()
    if not name or name[0].isdigit():
        name = "t_" + name
    return name or "data"


def read_table(csv_path: str | Path, header_names: list | None = None) -> dict:
    """解析 CSV 为结构化中间结果（不落库），便于单测与报告。"""
    csv_path = Path(csv_path)
    enc = detect_encoding(csv_path)
    with open(csv_path, encoding=enc, newline="") as f:
        text = f.read()
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        raise ValueError(f"CSV 为空：{csv_path}")
    delim = detect_delimiter(lines[:20])
    rows = [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=delim)
            if any(c.strip() for c in r)]
    if not rows:
        raise ValueError(f"CSV 没有有效数据行：{csv_path}")

    if header_names:
        header, data, detected = list(header_names), rows, False
    else:
        detected = has_header(rows)
        if detected:
            header, data = rows[0], rows[1:]
        else:
            header, data = [f"col_{i + 1}" for i in range(len(rows[0]))], rows
    if not data:
        raise ValueError(f"CSV 只有表头没有数据行：{csv_path}")

    ncols = max(len(header), max(len(r) for r in data[:200]))
    cols = clean_columns(header, ncols)
    types = infer_types(data, ncols)
    norm = [(list(r) + [""] * ncols)[:ncols] for r in data]
    return {"encoding": enc, "delimiter": delim, "header_detected": detected,
            "columns": cols, "types": types, "rows": norm, "row_count": len(norm)}


def import_csv(csv_path: str | Path, db_path: str | Path, table: str | None = None,
               header_names: list | None = None) -> dict:
    """把单个 CSV 导入为 SQLite 库中的一张表。返回导入摘要（不含任何凭据）。"""
    parsed = read_table(csv_path, header_names=header_names)
    table = table or sanitize_table_name(Path(csv_path).stem)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()          # 干净重建，避免旧表残留

    cols, types = parsed["columns"], parsed["types"]
    conn = sqlite3.connect(db_path)
    try:
        col_defs = ", ".join(f'"{c}" {t}' for c, t in zip(cols, types))
        conn.execute(f'CREATE TABLE "{table}" ({col_defs})')
        ph = ", ".join("?" for _ in cols)
        conn.executemany(
            f'INSERT INTO "{table}" VALUES ({ph})',
            [tuple(coerce(v, t) for v, t in zip(r, types)) for r in parsed["rows"]],
        )
        conn.commit()
        n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        conn.close()

    return {"table": table, "db_path": str(db_path), "row_count": n,
            "columns": list(zip(cols, types)), **{k: parsed[k] for k in
            ("encoding", "delimiter", "header_detected")}}
