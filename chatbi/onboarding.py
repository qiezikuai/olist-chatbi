"""数据源接入（onboarding）：把一个陌生 MySQL 库变成可问数的数据集。

五步流程（入口 scripts/onboard.py，本模块是其实现正源）：
  ① 连库提取全部基表的完整 DDL + 行数 + 外键关系   → datasets/<name>/schema.md
  ② LLM 读表结构生成**候选**指标口径              → datasets/<name>/metrics.md
  ③ LLM 生成 N 组问答对，标准 SQL 逐条实跑验证     → datasets/<name>/qa.yaml
  ④ 用这些材料训练该数据集独立的向量库            → datasets/<name>/chroma/
  ⑤ 抽 3 题走完整引擎链路，与标准 SQL 结果比对     → datasets/<name>/onboarding_report.md

自动化边界（README 同步说明，勿夸大）：
  **自动且是事实**：表结构、行数、外键（读自 SHOW CREATE TABLE / COUNT(*) / information_schema）。
  **自动但已验证**：问答对的标准 SQL——每条经只读账号实跑，跑不通的不进训练材料。
  **需人工确认**：指标口径。口径是业务定义（"活跃用户"怎么算、金额含不含税、要不要排除
    取消单），LLM 只能给候选。未经确认的口径**不会**被当作守卫规则强制生效——非内置
    数据集的 caliber_rules 为空（见 chatbi/datasets.py），因为拿未确认的口径去强制重写
    SQL 会把本来正确的查询改错。

凭据约束：密码由调用方经 getpass 或 config/db_*.env 提供，本模块绝不打印；发给 LLM 的
只有表结构与口径文本，连接信息与授权信息一律不进上下文。
"""
from __future__ import annotations

import getpass
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import pymysql
import yaml

from chatbi import knowledge
from chatbi.comparator import results_match
from chatbi.datasets import CONFIG_DIR, DATASETS_DIR, Materials
from chatbi.engine import ChatBIEngine
from chatbi.executor import ReadOnlyExecutor
from chatbi.secrets import read_db_config, read_llm_key

DEFAULT_QA_COUNT = 12
TRIAL_QUESTIONS = 3
MAX_BRIEF_CHARS = 60_000        # 发给 LLM 的表结构摘要上限，超出截断
DATASET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# 生成材料会入库，不得带 LLM 套话/自我指涉。这份清单本身可以公开。
AI_TELL_WORDS = ("作为一个AI", "作为 AI", "作为一个语言模型", "作为语言模型",
                 "我无法", "根据您的需求", "作为一个助手")
# 项目内部协作用语清单放在 gitignore 的本地文件里：清单若随代码入库，
# 等于把内部用语本身写进公开仓库，与"不得出现"的目的相反。
LOCAL_FORBIDDEN_FILE = CONFIG_DIR / "forbidden_words.txt"


# ============================== 数据结构 ==============================
@dataclass
class TableInfo:
    name: str
    rows: int
    rows_exact: bool
    ddl: str
    comment: str = ""
    columns: list = field(default_factory=list)   # [(列名, 类型, 可空, 键, 注释)]


@dataclass
class SchemaInfo:
    database: str
    tables: list = field(default_factory=list)
    fks: list = field(default_factory=list)       # [{table,column,ref_table,ref_column}]
    views: list = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(t.rows for t in self.tables)


# ============================== ① 提取表结构 ==============================
def extract_schema(cfg: dict) -> SchemaInfo:
    """连库提取基表 DDL、行数、列信息与外键关系。只发只读语句。"""
    db = cfg["database"]
    conn = pymysql.connect(host=cfg["host"], port=int(cfg.get("port", 3306)),
                           user=cfg["user"], password=cfg["password"],
                           database=db, charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            # 大表 COUNT(*) 可能很慢：设服务端超时，超时则退回 information_schema 估算值
            cur.execute("SET SESSION MAX_EXECUTION_TIME=30000")

            cur.execute("SHOW FULL TABLES")
            all_tables = cur.fetchall()
            base = sorted(r[0] for r in all_tables if r[1] == "BASE TABLE")
            views = sorted(r[0] for r in all_tables if r[1] != "BASE TABLE")
            if not base:
                raise RuntimeError(f"库 {db} 里没有基表，无法接入")

            cur.execute("SELECT TABLE_NAME, TABLE_ROWS, TABLE_COMMENT FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA=%s", (db,))
            meta = {r[0]: (r[1] or 0, r[2] or "") for r in cur.fetchall()}

            cur.execute("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, "
                        "COLUMN_COMMENT FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
                        "ORDER BY TABLE_NAME, ORDINAL_POSITION", (db,))
            cols: dict = {}
            for t, c, ty, nu, k, cm in cur.fetchall():
                cols.setdefault(t, []).append((c, ty, nu, k, cm or ""))

            info = SchemaInfo(database=db, views=views)
            for t in base:
                cur.execute(f"SHOW CREATE TABLE `{t}`")
                ddl = cur.fetchone()[1]
                exact = True
                try:
                    cur.execute(f"SELECT COUNT(*) FROM `{t}`")
                    n = cur.fetchone()[0]
                except pymysql.MySQLError:
                    exact = False
                    n = meta.get(t, (0, ""))[0]      # 退回估算行数
                info.tables.append(TableInfo(name=t, rows=n, rows_exact=exact, ddl=ddl,
                                             comment=meta.get(t, (0, ""))[1],
                                             columns=cols.get(t, [])))

            cur.execute("SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                        "FROM information_schema.KEY_COLUMN_USAGE "
                        "WHERE TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME IS NOT NULL "
                        "ORDER BY TABLE_NAME, COLUMN_NAME", (db,))
            info.fks = [{"table": a, "column": b, "ref_table": c, "ref_column": d}
                        for a, b, c, d in cur.fetchall()]
        return info
    finally:
        conn.close()


def warn_if_writable(cfg: dict) -> list:
    """返回该账号具备的写权限关键字。只读账号是设计前提（见 DECISIONS D3），越权要明确提示。"""
    conn = pymysql.connect(host=cfg["host"], port=int(cfg.get("port", 3306)),
                           user=cfg["user"], password=cfg["password"], charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW GRANTS")
            grants = "\n".join(r[0] for r in cur.fetchall())
    finally:
        conn.close()
    return sorted({m.group(0).upper() for m in re.finditer(
        r"\b(ALL PRIVILEGES|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|GRANT OPTION)\b", grants, re.I)})


def fk_lines(info: SchemaInfo) -> list:
    return [f"{fk['table']}.{fk['column']} -> {fk['ref_table']}.{fk['ref_column']}" for fk in info.fks]


def write_schema_md(ds_dir: Path, info: SchemaInfo) -> Path:
    """写 datasets/<name>/schema.md。

    DDL 用 ```sql 围栏（chatbi/datasets.py 按围栏提取完整 DDL 作为第 1 路训练材料）；
    外键清单用**无语言标注**的围栏，避免被当成 DDL 解析。
    """
    L = [f"# {info.database} 库表结构（接入时从库实时提取）", "",
         f"> 由 `scripts/onboard.py` 于 {time.strftime('%Y-%m-%d %H:%M')} 生成："
         f"DDL 取自 SHOW CREATE TABLE、行数为 COUNT(*) 实测、外键取自 information_schema。",
         f"> 共 {len(info.tables)} 张基表 / {info.total_rows:,} 行"
         + (f"；另有 {len(info.views)} 个视图（未纳入训练材料）" if info.views else ""), "",
         "## 表关系（外键约束，读自库、非推测）", ""]
    if info.fks:
        L += ["```"] + fk_lines(info) + ["```"]
    else:
        L.append("该库未声明外键约束——JOIN 关系需人工确认后补进 metrics.md，否则多表问题易错。")

    L += ["", "| 表 | 行数 | 表注释 |", "|---|---:|---|"]
    for t in info.tables:
        n = f"{t.rows:,}" + ("" if t.rows_exact else "（估算）")
        L.append(f"| {t.name} | {n} | {t.comment or '—'} |")

    for t in info.tables:
        n = f"{t.rows:,}" + ("" if t.rows_exact else "（估算）")
        L += ["", f"## {t.name}（{n} 行）", "", "```sql", t.ddl, "```"]

    p = ds_dir / "schema.md"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return p


def schema_brief(info: SchemaInfo) -> str:
    """给 LLM 看的紧凑表结构摘要：比原始 DDL 省 token，且带列注释与键信息。"""
    parts = []
    for t in info.tables:
        head = f"表 {t.name}（{t.rows:,} 行{'' if t.rows_exact else '，估算'}）"
        if t.comment:
            head += f"：{t.comment}"
        lines = [head]
        for c, ty, nu, k, cm in t.columns:
            tag = {"PRI": " PK", "UNI": " UNIQUE", "MUL": " 有索引"}.get(k, "")
            note = f"  -- {cm}" if cm else ""
            lines.append(f"  {c} {ty}{' NULL' if nu == 'YES' else ' NOT NULL'}{tag}{note}")
        parts.append("\n".join(lines))
    brief = "\n\n".join(parts)
    if info.fks:
        brief += "\n\n外键关系：\n" + "\n".join(f"  {line}" for line in fk_lines(info))
    if len(brief) > MAX_BRIEF_CHARS:
        brief = brief[:MAX_BRIEF_CHARS] + "\n\n（表结构过长，此处已截断）"
    return brief


# ============================== LLM 调用 ==============================
def llm_text(client, model: str, system: str, user: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return (resp.choices[0].message.content or "").strip()


def llm_json(client, model: str, system: str, user: str):
    """要 JSON 并稳健解析：去围栏、从首个 [ 或 { 起 raw_decode，容忍尾部多余文本。"""
    raw = llm_text(client, model, system, user)
    t = re.sub(r"^```(?:json)?", "", raw.strip()).strip()
    t = re.sub(r"```$", "", t).strip()
    starts = [i for i in (t.find("["), t.find("{")) if i >= 0]
    if not starts:
        raise RuntimeError(f"LLM 输出里找不到 JSON：{raw[:200]}")
    obj, _ = json.JSONDecoder().raw_decode(t[min(starts):])
    return obj


def gen_metrics(client, model: str, info: SchemaInfo) -> str:
    system = (
        "你是数据仓库建模顾问。根据用户给出的**真实**表结构与外键关系，为这个数据库拟定一份"
        "指标口径候选清单，供后续自然语言问数使用。\n"
        "硬性要求：\n"
        "1. 只依据给出的表和列，绝不臆造不存在的表名、列名或取值；\n"
        "2. 严格区分两类内容并分别标注：【事实】=能从表结构直接推出的（如主键、外键、"
        "日期列、金额列、枚举列）；【待确认】=需要业务方拍板的口径假设（如统计是否排除"
        "取消/退款、金额含税与否、去重按哪个键、时间基准用哪个日期列）；\n"
        "3. 覆盖：核心业务实体与粒度（一行代表什么）、时间基准候选、金额/数量口径候选、"
        "去重陷阱（1:N 关联后 COUNT 会放大）、常用分析维度；\n"
        "4. 不要编造任何具体数字或统计结果；\n"
        "5. 输出中文 markdown 正文，不要外层代码围栏，不要寒暄。"
    )
    user = f"数据库名：{info.database}\n\n{schema_brief(info)}\n\n请输出口径候选清单。"
    return llm_text(client, model, system, user)


def gen_qa(client, model: str, info: SchemaInfo, metrics: str, n: int) -> list:
    system = (
        "你是 MySQL 专家兼数据分析师。根据用户给出的**真实**表结构、外键关系与口径候选，"
        f"为这个数据库设计 {n} 组「中文业务问题 + 标准 SQL」，用作自然语言问数的训练示例。\n"
        "硬性要求：\n"
        "1. 只用给出的基表和列，绝不臆造；SQL 必须是 MySQL 8 可直接执行的**单条 SELECT**；\n"
        "2. 不要用视图、存储过程、自定义函数；不要写多语句；不要用 SELECT INTO；\n"
        "3. 题型必须覆盖：单表聚合、多表关联（按外键 JOIN）、排序取 TOP-N；"
        "若存在日期列则再加时序（按月/年分组）类；\n"
        "4. 1:N 关联后统计主体数量要用 COUNT(DISTINCT 主键)，不要 COUNT(*)；\n"
        "5. 问题用自然中文业务口吻，彼此不要只在字面上微调；\n"
        "6. 只输出 JSON 数组，不要 markdown 围栏、不要解释。元素形如 "
        '{"question": "...", "sql": "...", "type": "单表聚合|多表关联|排序对比|时序"}'
    )
    user = (f"数据库名：{info.database}\n\n【表结构】\n{schema_brief(info)}\n\n"
            f"【口径候选（其中【待确认】项不要当成既定事实）】\n{metrics[:6000]}\n\n"
            f"请输出 {n} 组问答对的 JSON 数组。")
    obj = llm_json(client, model, system, user)
    if isinstance(obj, dict):
        obj = obj.get("qa_pairs") or obj.get("data") or []
    out = []
    for item in obj:
        if isinstance(item, dict) and item.get("question") and item.get("sql"):
            out.append({"question": str(item["question"]).strip(),
                        "sql": str(item["sql"]).strip().rstrip(";"),
                        "type": str(item.get("type") or "未分类").strip()})
    return out


# ============================== ③ 实跑验证问答对 ==============================
def verify_qa(cfg: dict, qa_list: list) -> tuple:
    """逐条实跑标准 SQL。跑通的才进训练材料，跑不通的记入报告（不静默丢弃）。"""
    ex = ReadOnlyExecutor(host=cfg["host"], user=cfg["user"], password=cfg["password"],
                          database=cfg["database"], port=int(cfg.get("port", 3306)))
    good, bad = [], []
    try:
        for qa in qa_list:
            r = ex.execute(qa["sql"])
            if r.ok and r.row_count > 0:
                good.append({**qa, "verified": True, "rows": r.row_count})
            else:
                reason = (f"{r.error.code}: {r.error.message[:120]}" if (r.error and not r.ok)
                          else "执行成功但返回 0 行")
                bad.append({**qa, "verified": False, "reason": reason})
                print(f"      ✗ 淘汰：{qa['question'][:34]} —— {reason[:70]}")
    finally:
        ex.close()
    return good, bad


# ============================== 材料落盘 ==============================
def forbidden_words() -> tuple:
    """生效的禁词 = 通用 AI 套话（随代码公开）+ 本地清单（gitignore，可不存在）。"""
    extra: tuple = ()
    if LOCAL_FORBIDDEN_FILE.exists():
        lines = LOCAL_FORBIDDEN_FILE.read_text(encoding="utf-8").splitlines()
        extra = tuple(w.strip() for w in lines if w.strip() and not w.startswith("#"))
    return AI_TELL_WORDS + extra


def scan_forbidden(text: str, where: str) -> None:
    """生成材料会入库：出现禁词即中止落盘，不静默放过。"""
    hits = sorted({w for w in forbidden_words() if w in text})
    if hits:
        raise RuntimeError(f"{where} 含禁词 {hits}，已中止落盘（请重跑或手工清理）")


def write_metrics_md(ds_dir: Path, info: SchemaInfo, body: str) -> Path:
    header = [
        f"# {info.database} 指标口径（候选 · 待人工确认）", "",
        f"> 由 `scripts/onboard.py` 于 {time.strftime('%Y-%m-%d %H:%M')} 自动生成。",
        "> **口径是业务定义，自动生成的内容只是候选**：下面「表关系」一节读自库、属事实；",
        "> 其余标注【待确认】的口径假设必须由业务方核对后，才能当作正式口径使用。",
        "> 在人工确认前，这些口径**不会**被口径守卫当作强制规则（见 chatbi/datasets.py）。", "",
        "## 表关系（外键约束，读自库、属事实）", "",
    ]
    header += [f"- {line}" for line in fk_lines(info)] if info.fks else ["- 该库未声明外键约束"]
    text = "\n".join(header) + "\n\n## 口径候选\n\n" + body.strip() + "\n"
    scan_forbidden(text, "metrics.md")
    p = ds_dir / "metrics.md"
    p.write_text(text, encoding="utf-8")
    return p


def write_qa_yaml(ds_dir: Path, name: str, info: SchemaInfo, good: list) -> Path:
    payload = {
        "dataset": name,
        "database": info.database,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "标准 SQL 均经只读账号实跑验证可执行且返回非空结果；rows 为验证时行数",
        "qa_pairs": good,
    }
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=10_000_000)
    scan_forbidden(text, "qa.yaml")
    p = ds_dir / "qa.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# ============================== ⑤ 试跑 ==============================
def trial_run(name: str, good: list, k: int = TRIAL_QUESTIONS) -> list:
    """走完整引擎链路（检索→生成→执行→自纠错→守卫→总结），与标准 SQL 结果行级比对。"""
    picks = good[:k]
    out = []
    engine = ChatBIEngine(dataset=name)
    try:
        for qa in picks:
            ans = engine.ask(qa["question"])
            ref = engine.executor.execute(qa["sql"])
            if not ans.ok:
                e = ans.error
                rec = {"question": qa["question"], "ok": False,
                       "reason": f"链路失败 stage={ans.stage}" + (f" code={e.code}" if e else "")}
            elif not ref.ok:
                rec = {"question": qa["question"], "ok": False, "reason": "标准 SQL 复核失败"}
            else:
                matched, reason = results_match(ref.rows, ans.result.rows)
                rec = {"question": qa["question"], "ok": matched, "reason": "" if matched else reason,
                       "gen_sql": ans.sql, "ref_rows": ref.row_count,
                       "gen_rows": ans.result.row_count, "self_healed": ans.self_healed}
            out.append(rec)
            print(f"      {'✓' if rec['ok'] else '✗'} {qa['question'][:40]}"
                  + ("" if rec["ok"] else f" —— {rec['reason'][:70]}"))
    finally:
        engine.close()
    return out


def write_report(ds_dir: Path, name: str, info: SchemaInfo, counts: dict,
                 bad: list, trials: list, risky_grants: list) -> Path:
    passed = sum(1 for t in trials if t["ok"])
    L = [
        f"# {name} 数据源接入报告", "",
        f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}｜生成方式：`scripts/onboard.py`",
        f"> 目标库：`{info.database}`｜接入账号权限：{'仅 SELECT' if not risky_grants else '含写权限（见第 4 节）'}", "",
        "## 1. 库概况（读自库，属事实）", "",
        f"- 基表 **{len(info.tables)}** 张，合计 **{info.total_rows:,}** 行"
        + (f"；视图 {len(info.views)} 个（未纳入训练材料）" if info.views else ""),
        f"- 外键关系 **{len(info.fks)}** 条" + ("" if info.fks else "（该库未声明外键，JOIN 关系需人工确认）"),
        f"- 行数口径：{'全部 COUNT(*) 实测' if all(t.rows_exact for t in info.tables) else '部分表 COUNT 超时，退回 information_schema 估算'}", "",
        "| 表 | 行数 | 表注释 |", "|---|---:|---|",
    ]
    for t in info.tables:
        L.append(f"| {t.name} | {t.rows:,}{'' if t.rows_exact else '（估算）'} | {t.comment or '—'} |")

    L += ["", "## 2. 生成的训练材料", "",
          f"- 表结构 `schema.md`：{counts['ddl']} 条完整 DDL（含列定义，非仅表名行）",
          f"- 口径候选 `metrics.md`：{counts['metrics_chars']:,} 字，**全部标注为待人工确认**",
          f"- 问答对 `qa.yaml`：LLM 生成 {counts['qa_gen']} 组 → 实跑验证通过 **{counts['qa_good']}** 组"
          f"（淘汰 {counts['qa_bad']} 组）",
          "- 向量库 `chroma/`：三路材料训练，与其他数据集相互独立", ""]

    if bad:
        L += ["### 被淘汰的问答对（标准 SQL 实跑未通过，不进训练材料）", "",
              "| 问题 | 淘汰原因 |", "|---|---|"]
        L += [f"| {b['question'][:60]} | {b['reason'][:110]} |" for b in bad]
        L.append("")

    L += ["## 3. 端到端试跑（走完整引擎链路，与标准 SQL 结果行级比对）", "",
          f"**{passed}/{len(trials)} 通过**", "",
          "| 问题 | 结果 | 生成行数 | 参考行数 | 说明 |", "|---|---|---:|---:|---|"]
    for t in trials:
        L.append(f"| {t['question'][:50]} | {'✓ 通过' if t['ok'] else '✗ 未过'} "
                 f"| {t.get('gen_rows', '—')} | {t.get('ref_rows', '—')} | {t.get('reason', '')[:80] or '—'} |")

    if risky_grants:
        L += ["", "## 4. 安全提示", "",
              f"接入所用账号具备写权限：{', '.join(risky_grants)}。",
              "本项目的设计前提是**最小权限只读账号**（应用层白名单 + DB 只读双保险，见 "
              "docs/DECISIONS.md D3/D6）。请为该库另建仅 SELECT 的账号，并更新 "
              f"`config/db_{name}.env`。"]

    L += ["", "## 自动化边界（哪些自动、哪些必须人工）", "",
          "| 环节 | 程度 | 说明 |", "|---|---|---|",
          "| 表结构 / 行数 / 外键 | **自动·事实** | 读自 SHOW CREATE TABLE、COUNT(*)、information_schema |",
          "| 问答对标准 SQL | **自动·已验证** | 每条经只读账号实跑，跑不通即淘汰、不入训练材料 |",
          "| 向量库训练 | **自动** | 三路材料 + 固定时序（干净重建→训练→落盘等待→重实例化） |",
          "| 指标口径 | **候选·需人工确认** | 口径是业务定义，LLM 只能给假设；确认前不作为守卫规则生效 |",
          "| JOIN 语义 / 去重键 | **需人工确认** | 外键只给结构关系，业务上该怎么 JOIN、按什么去重仍需人判断 |",
          "| 评估集与准确率 | **需人工** | 本次仅 3 题试跑证明链路通，不等于准确率；正式评估需另建留出集 |",
          "", "> 换句话说：接入一个库能自动跑通问数，但**口径正确性仍需业务方确认**——"
          "这部分不是脚本能替代的。", ""]

    text = "\n".join(L) + "\n"
    scan_forbidden(text, "onboarding_report.md")
    p = ds_dir / "onboarding_report.md"
    p.write_text(text, encoding="utf-8")
    return p


# ============================== 连接信息 ==============================
def prompt_connection(default_db: str = "") -> dict:
    print("请输入目标库的连接信息（建议使用**仅 SELECT** 的最小权限账号）：")
    host = input("  主机 [127.0.0.1]: ").strip() or "127.0.0.1"
    port = input("  端口 [3306]: ").strip() or "3306"
    user = input("  用户名: ").strip()
    pwd = getpass.getpass("  密码（输入不回显）: ")
    db_hint = f" [{default_db}]" if default_db else ""
    db = input(f"  数据库名{db_hint}: ").strip() or default_db
    if not user or not db:
        raise RuntimeError("用户名与数据库名不能为空")
    return {"host": host, "port": port, "user": user, "password": pwd, "database": db}


def save_db_env(name: str, cfg: dict) -> Path:
    """写 config/db_<name>.env（gitignore）——引擎按此约定解析数据集连接信息。"""
    p = CONFIG_DIR / f"db_{name}.env"
    p.parent.mkdir(exist_ok=True)
    p.write_text(f"host={cfg['host']}\nport={cfg.get('port', 3306)}\nuser={cfg['user']}\n"
                 f"password={cfg['password']}\ndatabase={cfg['database']}\n", encoding="utf-8")
    return p


def normalize_dataset_name(name: str) -> str:
    """数据集名会拼进文件路径，必须限制字符集（防路径穿越）。"""
    n = (name or "").strip().lower()
    if not DATASET_NAME_RE.match(n):
        raise ValueError(f"数据集名 '{name}' 不合法：只允许小写字母、数字、下划线、连字符，"
                         f"且以字母或数字开头，长度 ≤32")
    return n


# ============================== 主流程 ==============================
def run(args) -> int:
    if args.db_env:
        env_path = Path(args.db_env)
        if not env_path.is_absolute():
            env_path = CONFIG_DIR.parent / env_path
        cfg = read_db_config(env_path)
        name = args.dataset or env_path.stem.replace("db_", "", 1)
    else:
        cfg = prompt_connection()
        name = args.dataset or cfg["database"]

    try:
        name = normalize_dataset_name(name)
    except ValueError as e:
        print(f"✗ {e}")
        return 2

    ds_dir = DATASETS_DIR / name
    ds_dir.mkdir(parents=True, exist_ok=True)
    env_out = save_db_env(name, cfg)      # 引擎按约定从 config/db_<name>.env 取连接信息
    print(f"\n数据集：{name}｜目标库：{cfg['database']}｜材料目录：{ds_dir}")
    print(f"连接凭据：{env_out}（已被 .gitignore 排除）")

    risky = warn_if_writable(cfg)
    if risky:
        print(f"⚠ 该账号具备写权限（{', '.join(risky)}）；本项目设计前提是仅 SELECT 的只读账号，"
              f"建议另建最小权限账号。仍将继续，但会记入接入报告的安全提示。")

    print("\n[① /5] 提取表结构（DDL + 行数 + 外键）…")
    info = extract_schema(cfg)
    schema_path = write_schema_md(ds_dir, info)
    print(f"      ✓ {len(info.tables)} 张基表 / {info.total_rows:,} 行 / {len(info.fks)} 条外键 "
          f"→ {schema_path.name}")

    key = read_llm_key()
    client = knowledge.new_llm_client(key)          # 注入 base_url，见 DECISIONS D2
    model = knowledge.DEFAULT_MODEL

    print(f"\n[② /5] LLM 生成候选指标口径（{model}）…")
    metrics_body = gen_metrics(client, model, info)
    metrics_path = write_metrics_md(ds_dir, info, metrics_body)
    print(f"      ✓ {len(metrics_body):,} 字 → {metrics_path.name}（全部标注为待人工确认）")

    print(f"\n[③ /5] LLM 生成 {args.qa_count} 组问答对，并逐条实跑验证…")
    qa_raw = gen_qa(client, model, info, metrics_body, args.qa_count)
    print(f"      生成 {len(qa_raw)} 组，开始验证：")
    good, bad = verify_qa(cfg, qa_raw)
    qa_path = write_qa_yaml(ds_dir, name, info, good)
    print(f"      ✓ 验证通过 {len(good)}/{len(qa_raw)} 组 → {qa_path.name}")
    if not good:
        print("✗ 没有任何问答对通过实跑验证，无法训练。请检查账号权限与库内容。")
        return 1

    print(f"\n[④ /5] 训练该数据集独立向量库 → {ds_dir / 'chroma'}")
    materials = Materials(ddl=[t.ddl for t in info.tables],
                          documentation=metrics_path.read_text(encoding="utf-8").strip(),
                          qa_pairs=[{"question": q["question"], "sql": q["sql"]} for q in good])
    knowledge.build(llm_key=key, db_cfg=cfg, chroma_dir=ds_dir / "chroma", materials=materials)

    trials = []
    if args.skip_trial:
        print("\n[⑤ /5] 已按 --skip-trial 跳过试跑")
    else:
        print(f"\n[⑤ /5] 抽 {TRIAL_QUESTIONS} 题走完整引擎链路试跑…")
        trials = trial_run(name, good)

    report = write_report(ds_dir, name, info,
                          counts={"ddl": len(info.tables), "metrics_chars": len(metrics_body),
                                  "qa_gen": len(qa_raw), "qa_good": len(good), "qa_bad": len(bad)},
                          bad=bad, trials=trials, risky_grants=risky)

    passed = sum(1 for t in trials if t["ok"])
    print(f"\n{'=' * 64}")
    print(f"接入完成：{name}（库 {info.database}）")
    print(f"  材料：{schema_path.name} / {metrics_path.name} / {qa_path.name}")
    print(f"  向量库：{ds_dir / 'chroma'}")
    if trials:
        print(f"  试跑：{passed}/{len(trials)} 通过")
    print(f"  报告：{report}")
    print(f"\n下一步：uv run python main.py --dataset {name} \"你的问题\"")
    print("提醒：metrics.md 是候选口径，请人工确认后再作为正式业务口径使用。")
    print("=" * 64)
    return 0
