"""只读 SQL 执行闸——编排层唯一的执行通道，支持 MySQL 与 SQLite 两种后端。

四道防线（两种后端共用同一条流水线，见 BaseExecutor.execute）：
  1. 语句白名单：只放行单条 SELECT / WITH...SELECT；DML/DDL、多语句、
     INTO OUTFILE/DUMPFILE、LOAD_FILE 一律在应用层拒绝（先于 DB）。
  2. 强制 LIMIT：无 LIMIT 的查询自动追加 LIMIT，防百万行回传打爆内存。
  3. 超时：MySQL 用会话级 MAX_EXECUTION_TIME（服务端掐断）+ 连接 read_timeout 兜底；
     SQLite 没有服务端超时，用 set_progress_handler 在 Python 层按截止时间中断。
  4. 错误归一化：任何 DB 异常 → 结构化 SqlError(code/errno/stage/message)，供自纠错回填重写。

安全模型（双保险在两种后端上的对应物）：
  - MySQL：应用层白名单 + 只读账号 chatbi_ro（仅 SELECT，见 DECISIONS D3）。
  - SQLite：应用层白名单 + 以 `mode=ro` URI 只读打开库文件——即使白名单被绕过，
    写操作也会在读只层失败。权限层始终是硬闸，不靠 prompt。
红线：DB 凭据从 config/db_*.env 读取，绝不打印、绝不入 git；SQLite 无凭据，只有文件路径。
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent.parent

# 允许作为「语句第一个关键字」的集合
_ALLOWED_FIRST = {"SELECT", "WITH"}
# SELECT 也可能触发副作用的危险构造（写服务器文件 / 读服务器文件）
_DANGEROUS = ("INTO OUTFILE", "INTO DUMPFILE", "LOAD_FILE(")


@dataclass
class SqlError:
    """归一化后的执行错误。code 供上层按类别决策是否回喂 LLM 重写。"""
    code: str            # BLOCKED / TIMEOUT / SYNTAX / SEMANTIC / PERMISSION / DB_ERROR
    stage: str           # whitelist / connect / execute
    message: str
    errno: int | None = None


@dataclass
class SqlResult:
    """统一返回结构：ok=False 时看 error，ok=True 时看 rows/columns。不抛异常，便于编排层消费。"""
    ok: bool
    sql: str = ""                                   # 实际执行的 SQL（已做 LIMIT 处理）
    rows: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    row_count: int = 0
    elapsed_ms: int = 0
    error: SqlError | None = None


def _skeleton(sql: str) -> str:
    """挖空字符串字面量与注释，得到只含 SQL 结构的「骨架」，用于安全扫描。

    目的：字符串里的 ';'、'--'、'select' 等不会污染白名单/多语句判定。
    用单个空格占位，保留词边界。手写扫描而非正则，避免引号/注释嵌套误判。
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        # 行注释： -- 到行尾，或 # 到行尾
        if (c == '-' and i + 1 < n and sql[i + 1] == '-') or c == '#':
            while i < n and sql[i] != '\n':
                i += 1
            out.append(' ')
            continue
        # 块注释： /* ... */
        if c == '/' and i + 1 < n and sql[i + 1] == '*':
            i += 2
            while i + 1 < n and not (sql[i] == '*' and sql[i + 1] == '/'):
                i += 1
            i = min(i + 2, n)
            out.append(' ')
            continue
        # 字符串 / 标识符： ' " `
        if c in ("'", '"', '`'):
            quote = c
            i += 1
            while i < n:
                if sql[i] == '\\' and quote != '`':       # 反斜杠转义（反引号标识符内不转义）
                    i += 2
                    continue
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:  # '' / "" 双写转义
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(' ')
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def classify(sql: str) -> tuple[bool, str]:
    """白名单校验。返回 (是否放行, 原因)。纯函数，不连库、与方言无关。"""
    if not sql or not sql.strip():
        return False, "空 SQL"
    body = _skeleton(sql).strip().rstrip(';').strip()
    if not body:
        return False, "空 SQL（仅注释）"
    # 去尾分号后若仍含分号 → 多语句（防 SELECT 1; DROP TABLE ...）
    if ';' in body:
        return False, "疑似多语句（含内部分号），拒绝"
    m = re.match(r'[A-Za-z_]+', body)
    first_kw = m.group(0).upper() if m else ""
    if first_kw not in _ALLOWED_FIRST:
        return False, f"语句类型 {first_kw or '未知'} 不在白名单（仅 SELECT/WITH）"
    up = body.upper()
    for d in _DANGEROUS:
        if d in up:
            return False, f"含危险构造 {d.rstrip('(')}，拒绝"
    return True, "ok"


def force_limit(sql: str, max_rows: int) -> tuple[str, bool]:
    """无 LIMIT 则追加。返回 (最终 SQL, 是否注入了 LIMIT)。

    仅在「整条语句未出现 LIMIT」时注入；已含 LIMIT（含子查询内）则信任之、不重复追加
    （重复 LIMIT 会语法错）。MySQL 与 SQLite 的 LIMIT 语法一致，故此函数两后端共用。
    """
    body = sql.strip().rstrip(';').rstrip()
    if re.search(r'\bLIMIT\b', _skeleton(sql).upper()):
        return body, False
    return f"{body} LIMIT {int(max_rows)}", True


class BaseExecutor:
    """只读执行闸的公共流水线。子类只提供「怎么连、怎么跑、怎么翻译错误」。"""

    backend = "base"
    _db_errors: tuple = (Exception,)

    def __init__(self, timeout_s: int = 10, max_rows: int = 1000):
        self.timeout_s = timeout_s
        self.max_rows = max_rows
        self._conn = None

    # ---------- 子类钩子 ----------
    def _connect(self):
        raise NotImplementedError

    def _run(self, conn, sql: str) -> tuple[list, list]:
        raise NotImplementedError

    def _normalize(self, e: Exception, stage: str) -> SqlError:
        raise NotImplementedError

    def _close_conn(self, conn) -> None:
        try:
            conn.close()
        except Exception:
            pass

    # ---------- 公共生命周期 ----------
    def close(self) -> None:
        if self._conn is not None:
            self._close_conn(self._conn)
            self._conn = None

    def __enter__(self) -> "BaseExecutor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- 公共流水线 ----------
    def execute(self, sql: str, max_rows: int | None = None) -> SqlResult:
        """执行一条只读查询，四道防线依次生效，永不抛异常（错误归一化进 SqlResult.error）。"""
        # 防线 1：白名单
        allowed, reason = classify(sql)
        if not allowed:
            return SqlResult(ok=False, sql=sql,
                             error=SqlError(code='BLOCKED', stage='whitelist', message=reason))
        # 防线 2：强制 LIMIT
        final_sql, _injected = force_limit(sql, max_rows if max_rows is not None else self.max_rows)

        t0 = time.perf_counter()
        try:
            # 防线 3a：连接
            try:
                conn = self._connect()
            except self._db_errors as e:
                return SqlResult(ok=False, sql=final_sql, error=self._normalize(e, stage='connect'),
                                 elapsed_ms=int((time.perf_counter() - t0) * 1000))
            # 防线 3b：执行（各后端自管超时）；防线 4：错误归一化
            try:
                rows, cols = self._run(conn, final_sql)
                return SqlResult(ok=True, sql=final_sql, rows=rows, columns=cols,
                                 row_count=len(rows), elapsed_ms=int((time.perf_counter() - t0) * 1000))
            except self._db_errors as e:
                return SqlResult(ok=False, sql=final_sql, error=self._normalize(e, stage='execute'),
                                 elapsed_ms=int((time.perf_counter() - t0) * 1000))
        except Exception as e:  # 后端意外异常也归一化，兜住"永不抛异常"契约
            return SqlResult(ok=False, sql=final_sql,
                             error=SqlError(code='DB_ERROR', stage='execute',
                                            message=f"unexpected {type(e).__name__}: {str(e)[:200]}"),
                             elapsed_ms=int((time.perf_counter() - t0) * 1000))


def _normalize_mysql(e: pymysql.MySQLError, stage: str) -> SqlError:
    """把 pymysql 异常映射成结构化 SqlError。"""
    errno = e.args[0] if e.args and isinstance(e.args[0], int) else None
    msg = str(e).strip()
    low = msg.lower()
    if errno in (3024, 2013) or 'maximum statement execution time' in low or 'lost connection' in low:
        code = 'TIMEOUT'
    elif errno == 1064:
        code = 'SYNTAX'
    elif errno in (1142, 1143, 1044, 1045):
        code = 'PERMISSION'
    elif errno in (1054, 1146, 1052, 1060, 1066, 1109, 1241, 1051):
        # 未知列 / 表不存在 / 列歧义 / 重复列 / 未知表 等——语义错，可回喂 LLM 重写
        code = 'SEMANTIC'
    else:
        code = 'DB_ERROR'
    return SqlError(code=code, stage=stage, message=msg[:300], errno=errno)


class ReadOnlyExecutor(BaseExecutor):
    """MySQL 只读执行器：一次实例化，可复用一个连接（断连自动重连）。"""

    backend = "mysql"
    _db_errors = (pymysql.MySQLError,)

    def __init__(self, host: str, user: str, password: str, database: str,
                 port: int = 3306, timeout_s: int = 10, max_rows: int = 1000):
        super().__init__(timeout_s=timeout_s, max_rows=max_rows)
        self._conn_kwargs = dict(
            host=host, user=user, password=password, database=database, port=int(port),
            connect_timeout=max(3, min(timeout_s, 15)),
            read_timeout=timeout_s, write_timeout=timeout_s,
        )

    @classmethod
    def from_env(cls, env_path: str | Path | None = None, **kw) -> "ReadOnlyExecutor":
        """从 config/db_*.env 读取只读凭据构建执行器（解析实现见 chatbi/secrets）。"""
        from chatbi.secrets import read_db_config
        cfg = read_db_config(env_path)
        return cls(host=cfg['host'], user=cfg['user'], password=cfg['password'],
                   database=cfg['database'], port=cfg.get('port', 3306), **kw)

    def _connect(self) -> pymysql.connections.Connection:
        if self._conn is not None:
            try:
                self._conn.ping(reconnect=False)   # 存活探测；死连接抛错后手动重建（不用已弃用的 reconnect=True）
                return self._conn
            except Exception:
                self._close_conn(self._conn)
                self._conn = None
        conn = pymysql.connect(**self._conn_kwargs)
        with conn.cursor() as cur:
            # 会话级超时（毫秒）：服务端主动掐断超时 SELECT，比单纯客户端 read_timeout 更干净
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(self.timeout_s * 1000)}")
        self._conn = conn
        return conn

    def _run(self, conn, sql: str) -> tuple[list, list]:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
        return rows, cols

    def _normalize(self, e: Exception, stage: str) -> SqlError:
        return _normalize_mysql(e, stage)


def _normalize_sqlite(e: sqlite3.Error, stage: str) -> SqlError:
    """把 sqlite3 异常映射成与 MySQL 同构的 SqlError（上层按 code 决策，不关心后端）。"""
    msg = str(e).strip()
    low = msg.lower()
    if "interrupt" in low or "timeout" in low or "locked" in low:
        code = 'TIMEOUT'          # set_progress_handler 中断 / 锁等待超时
    elif "syntax error" in low or "malformed" in low:
        code = 'SYNTAX'
    elif "no such table" in low or "no such column" in low or "ambiguous" in low:
        code = 'SEMANTIC'         # 可回喂 LLM 重写
    elif "readonly" in low or "read-only" in low or "permission" in low:
        code = 'PERMISSION'       # mode=ro 下写操作的落点
    else:
        code = 'DB_ERROR'
    return SqlError(code=code, stage=stage, message=msg[:300], errno=None)


class SqliteExecutor(BaseExecutor):
    """SQLite 只读执行器：库文件以 mode=ro 打开，写操作在读只层即失败。

    SQLite 没有 MySQL 的 MAX_EXECUTION_TIME，超时靠 set_progress_handler：
    虚拟机每执行 N 条指令回调一次，超过截止时间返回非 0 即中断当前语句。
    """

    backend = "sqlite"
    _db_errors = (sqlite3.Error,)
    _PROGRESS_EVERY = 100_000     # 每 N 条虚拟机指令检查一次截止时间

    def __init__(self, path: str | Path, timeout_s: int = 10, max_rows: int = 1000):
        super().__init__(timeout_s=timeout_s, max_rows=max_rows)
        self._path = str(path)
        self._deadline = 0.0

    @classmethod
    def from_dataset_file(cls, db_path: str | Path, **kw) -> "SqliteExecutor":
        return cls(path=db_path, **kw)

    def _progress_hit(self) -> int:
        if time.perf_counter() > self._deadline:
            return 1              # 非 0 = 请求中断当前语句
        return 0

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        # mode=ro：只读打开，与 MySQL 只读账号同级的第二道闸（见模块注释）
        uri = f"file:{self._path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=max(3, min(self.timeout_s, 15)))
        conn.set_progress_handler(self._progress_hit, self._PROGRESS_EVERY)
        self._conn = conn
        return conn

    def _run(self, conn, sql: str) -> tuple[list, list]:
        self._deadline = time.perf_counter() + self.timeout_s
        cur = conn.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        return rows, cols

    def _normalize(self, e: Exception, stage: str) -> SqlError:
        return _normalize_sqlite(e, stage)


def executor_for_dataset(ds, timeout_s: int = 10, max_rows: int = 1000) -> BaseExecutor:
    """按数据集的后端构建对应执行器。MySQL 走凭据文件，SQLite 走库文件路径。"""
    if ds.backend == "sqlite":
        if ds.sqlite_path is None or not Path(ds.sqlite_path).exists():
            raise RuntimeError(f"数据集 '{ds.name}' 声明为 sqlite 后端但库文件不存在："
                               f"{ds.sqlite_path}；请重跑 scripts/onboard.py --csv")
        return SqliteExecutor(path=ds.sqlite_path, timeout_s=timeout_s, max_rows=max_rows)
    return ReadOnlyExecutor.from_env(env_path=ds.db_env_path,
                                     timeout_s=timeout_s, max_rows=max_rows)
