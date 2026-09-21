"""P2.1：只读 SQL 执行工具——编排层的「执行闸」。

四道防线（对应 PLAN_v2 P2.1 验收标准）：
  1. 语句白名单：只放行单条 SELECT / WITH...SELECT；DML/DDL、多语句、
     INTO OUTFILE/DUMPFILE、LOAD_FILE 一律在应用层拒绝（先于 DB）。
  2. 强制 LIMIT：无 LIMIT 的查询自动追加 LIMIT，防百万行回传打爆内存。
  3. 超时：会话级 MAX_EXECUTION_TIME（服务端掐断超时 SELECT）+ 连接 read_timeout（客户端兜底）。
  4. 错误归一化：任何 DB 异常 → 结构化 SqlError(code/errno/stage/message)，供 P2.3 自纠错回填重写。

安全模型：与 DB 只读账号 chatbi_ro（仅 SELECT 权限，见 DECISIONS D3）构成双保险——
应用层白名单挡 LLM 幻觉语句，DB 权限层兜底，任一层失效另一层仍在。
红线：DB 凭据从 config/db_ro.env 读取，绝不打印、绝不入 git。
"""
from __future__ import annotations

import re
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
    """归一化后的执行错误。code 供上层(P2.3 自纠错)按类别决策是否回喂 LLM 重写。"""
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
    """白名单校验。返回 (是否放行, 原因)。纯函数，不连库。"""
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
    （重复 LIMIT 会语法错）。max_rows 上限另由服务端超时兜底。
    """
    body = sql.strip().rstrip(';').rstrip()
    if re.search(r'\bLIMIT\b', _skeleton(sql).upper()):
        return body, False
    return f"{body} LIMIT {int(max_rows)}", True


def _normalize(e: pymysql.MySQLError, stage: str) -> SqlError:
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


class ReadOnlyExecutor:
    """只读 SQL 执行器：一次实例化，可复用一个连接（断连自动重连）。"""

    def __init__(self, host: str, user: str, password: str, database: str,
                 port: int = 3306, timeout_s: int = 10, max_rows: int = 1000):
        self.timeout_s = timeout_s
        self.max_rows = max_rows
        self._conn_kwargs = dict(
            host=host, user=user, password=password, database=database, port=int(port),
            connect_timeout=max(3, min(timeout_s, 15)),
            read_timeout=timeout_s, write_timeout=timeout_s,
        )
        self._conn: pymysql.connections.Connection | None = None

    @classmethod
    def from_env(cls, env_path: str | Path | None = None, **kw) -> "ReadOnlyExecutor":
        """从 config/db_ro.env 读取 chatbi_ro 只读凭据构建执行器（凭据不外泄）。"""
        path = Path(env_path) if env_path else ROOT / "config" / "db_ro.env"
        cfg: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1)
                cfg[k.strip()] = v.strip()
        return cls(host=cfg['host'], user=cfg['user'], password=cfg['password'],
                   database=cfg['database'], port=cfg.get('port', 3306), **kw)

    def _connect(self) -> pymysql.connections.Connection:
        if self._conn is not None:
            try:
                self._conn.ping(reconnect=False)   # 存活探测；死连接抛错后手动重建（不用已弃用的 reconnect=True）
                return self._conn
            except Exception:
                self._safe_close()
        conn = pymysql.connect(**self._conn_kwargs)
        with conn.cursor() as cur:
            # 会话级超时（毫秒）：服务端主动掐断超时 SELECT，比单纯客户端 read_timeout 更干净
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(self.timeout_s * 1000)}")
        self._conn = conn
        return conn

    def _safe_close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def close(self) -> None:
        self._safe_close()

    def __enter__(self) -> "ReadOnlyExecutor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

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
        # 防线 3a：连接（带 connect/read 超时）
        try:
            conn = self._connect()
        except pymysql.MySQLError as e:
            return SqlResult(ok=False, sql=final_sql, error=_normalize(e, stage='connect'),
                             elapsed_ms=int((time.perf_counter() - t0) * 1000))
        # 防线 3b：执行（会话 MAX_EXECUTION_TIME 掐断慢查询）；防线 4：错误归一化
        try:
            with conn.cursor() as cur:
                cur.execute(final_sql)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
            return SqlResult(ok=True, sql=final_sql, rows=rows, columns=cols,
                             row_count=len(rows), elapsed_ms=int((time.perf_counter() - t0) * 1000))
        except pymysql.MySQLError as e:
            return SqlResult(ok=False, sql=final_sql, error=_normalize(e, stage='execute'),
                             elapsed_ms=int((time.perf_counter() - t0) * 1000))
