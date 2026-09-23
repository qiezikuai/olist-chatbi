"""编排引擎——「检索 → 生成 → 执行 → 校验 →（自纠错/守卫）→ 总结」端到端链路。

编排流程自 P-LangGraph 起由 chatbi/graph.py 的 **StateGraph** 显式表达（节点=各步骤、条件边=路由）；
本模块负责：① 持有资源（vanna 生成器、ReadOnlyExecutor 执行闸、LLM client）② 提供被图节点复用的
helper（_repair_sql / _rewrite_sql / _summarize / _log_trace）③ 把图的最终 state 映射回 Answer。

四层不变：数据层(MySQL chatbi_ro) / 知识层(三路训练+chroma) / 生成层(vanna) / 评估层(eval) 均不动；
executor.py(执行闸)、guards.py(守卫)、comparator.py(比对器) 原样复用、由图节点包装，不重写。

为什么用 LangGraph、StateGraph 如何映射原编排：见 docs/DECISIONS.md D7。
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from chatbi.executor import ReadOnlyExecutor, SqlError, SqlResult

ROOT = Path(__file__).resolve().parent.parent

# 可触发「回喂重写」的错误类别：语义/语法错可由 LLM 修正，超时可让其缩小范围，其余 DB 错给一次机会。
# 不可重试：BLOCKED（白名单安全拒绝，重写=试图绕过，必须终止）、PERMISSION（只读账号设计如此）、
#           GENERATE_FAIL/EMPTY_SQL（生成阶段问题，不在 SQL 修复范畴）。
RETRIABLE_CODES = {"SEMANTIC", "SYNTAX", "TIMEOUT", "DB_ERROR"}


def is_retriable_error(code: str) -> bool:
    """该错误码是否值得回喂 LLM 重写一轮。纯函数，便于单测；被 graph.py 的路由复用。"""
    return code in RETRIABLE_CODES


def _clean_sql(text: str) -> str:
    """从 LLM 重写回复里提取出干净 SQL：去 markdown 围栏、取第一条 SELECT/WITH、截到分号前。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:sql)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    m = re.search(r"\b(SELECT|WITH)\b", t, re.IGNORECASE)
    if m:
        t = t[m.start():]
    return t.split(";")[0].strip()


def _read_llm_key(env_path: str | Path | None = None) -> str:
    """从 .env 读 SiliconFlow Key（绝不打印、绝不入 git）。"""
    p = Path(env_path) if env_path else ROOT / ".env"
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("SILICONFLOW_API_KEY="):
            k = line.split("=", 1)[1].strip()
            if k and k != "your_key_here":
                return k
    raise RuntimeError("SILICONFLOW_API_KEY 未就绪（.env 未填或为占位符）")


@dataclass
class Answer:
    """一次问数的完整结果。ok=False 时看 stage（停在哪步）+ error。"""
    ok: bool
    question: str
    sql: str = ""                          # 实际执行的 SQL（已加 LIMIT；自愈后为重写版）
    summary: str = ""                      # 结论文本
    result: SqlResult | None = None        # 执行结果（rows/columns/row_count）
    stage: str = ""                        # generate / execute / ok
    error: SqlError | None = None
    self_healed: bool = False              # 是否经 1 轮自纠错后才成功
    trace: dict = field(default_factory=dict)   # 自纠错留痕（attempt0/attempt1/final）
    caliber_violations: list = field(default_factory=list)  # P2.4 口径守卫：最终仍违反的口径（空=命中）
    empty_retried: bool = False            # P2.4 空结果守卫：是否因 0 行触发过改写
    guard_trace: dict = field(default_factory=dict)   # P2.4 守卫留痕（供 P3.3 报告统计贡献）


class ChatBIEngine:
    """问数引擎：vanna 负责「检索+生成」，ReadOnlyExecutor 负责「执行」，编排流程由 StateGraph 表达。"""

    def __init__(self, chroma_path: str | Path | None = None,
                 model: str = "deepseek-ai/DeepSeek-V3.2",
                 timeout_s: int = 10, max_rows: int = 1000,
                 llm_key: str | None = None):
        from openai import OpenAI
        from vanna.openai import OpenAI_Chat
        from vanna.chromadb import ChromaDB_VectorStore

        chroma_dir = Path(chroma_path) if chroma_path else ROOT / "chroma"
        # 冷加载已训练的向量库（D5：不训练、直接加载实测正常）。未训练则给出明确指引。
        if not chroma_dir.exists() or not any(chroma_dir.iterdir()):
            raise RuntimeError(
                f"未找到已训练的向量库（{chroma_dir}）。请先运行训练："
                f"`uv run python scripts/train.py`（或 scripts/train_and_test.py）。"
            )

        class _MyVanna(ChromaDB_VectorStore, OpenAI_Chat):
            def __init__(self, client, config):
                ChromaDB_VectorStore.__init__(self, config=config)
                OpenAI_Chat.__init__(self, client=client, config=config)

        key = llm_key or _read_llm_key()
        self.model = model
        self._client = OpenAI(api_key=key, base_url="https://api.siliconflow.cn/v1",
                              timeout=90, max_retries=2)
        # vanna 只用于「生成 SQL / 检索 DDL」，不调 connect_to_mysql / run_sql —— 执行走自有只读闸
        self.vn = _MyVanna(self._client, {"model": model, "path": str(chroma_dir), "language": "中文"})
        self.executor = ReadOnlyExecutor.from_env(timeout_s=timeout_s, max_rows=max_rows)
        self.logs_dir = ROOT / "logs"

        # 编排状态图（延迟 import 打破 engine↔graph 的模块级循环：graph 在 module 级 import 本模块的纯函数）
        from chatbi.graph import build_graph
        self._graph = build_graph(self)

    # ---------- 对外入口 ----------
    def ask(self, question: str) -> Answer:
        """自然语言问数：从 generate 入图，走完整链路。"""
        return self._state_to_answer(question, self._invoke({"question": question}))

    def run_sql(self, question: str, sql: str) -> Answer:
        """从 execute 入图（跳过生成）：注入指定 SQL 走 执行→自纠错→守卫→总结。

        供 demo/调试与验收用（人为构造错误 SQL 触发自愈、构造违规 SQL 触发守卫）。
        """
        return self._state_to_answer(question, self._invoke({"question": question, "sql": sql}))

    def _invoke(self, init: dict) -> dict:
        base = {"repairs": 0, "empty_retried": False, "caliber_retried": False, "guard_trace": {}}
        base.update(init)
        return self._graph.invoke(base, config={"recursion_limit": 50})

    def _state_to_answer(self, question: str, final: dict) -> Answer:
        return Answer(
            ok=final.get("ok", False),
            question=question,
            sql=final.get("sql", ""),
            summary=final.get("final_answer", ""),
            result=final.get("result"),
            stage=final.get("stage", ""),
            error=final.get("error"),
            self_healed=final.get("self_healed", False),
            trace=final.get("trace", {}),
            caliber_violations=final.get("guard_violations", []),
            empty_retried=final.get("empty_retried", False),
            guard_trace=final.get("guard_trace", {}),
        )

    # ---------- 被图节点复用的 helper ----------
    def _repair_sql(self, question: str, bad_sql: str, error: SqlError) -> str | None:
        """P2.3：把执行报错回喂 LLM 重写。是 _rewrite_sql 的错误专用包装。"""
        reason = f"在 MySQL 上执行报错，code={error.code}（errno={error.errno}）：{error.message}"
        return self._rewrite_sql(question, bad_sql, reason)

    def _rewrite_sql(self, question: str, sql: str, reason: str) -> str | None:
        """通用重写：把「当前 SQL + 需要修正的原因 + 相关 DDL」回喂 LLM，要一条修正后的 SELECT。

        自纠错（执行报错）与守卫（空结果/口径违规）共用此入口。失败返回 None。
        产物不直接采信——调用方一律再经 ReadOnlyExecutor 执行闸二次安检。
        """
        ddl_hint = ""
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ddls = self.vn.get_related_ddl(question) or []
            ddl_hint = "\n".join(str(d) for d in ddls[:6])
        except Exception:
            ddl_hint = ""
        messages = [
            {"role": "system", "content":
                "你是 MySQL 专家。请依据给出的原因和表结构修正下面这条 SQL。"
                "只输出一条修正后的、可执行的 SELECT 语句，不要解释、不要 markdown 围栏。"},
            {"role": "user", "content":
                f"业务问题：{question}\n"
                f"当前 SQL：{sql}\n"
                f"需要修正的原因：{reason}\n"
                f"相关表结构：\n{ddl_hint}\n"
                f"请输出修正后的 SELECT 语句："},
        ]
        try:
            resp = self._client.chat.completions.create(model=self.model, messages=messages)
            return _clean_sql(resp.choices[0].message.content)
        except Exception:
            return None

    def _log_trace(self, trace: dict) -> None:
        """把一次自纠错/守卫留痕追加到 logs/self_correction.jsonl（logs/ 已 gitignore，运行产物不入库）。"""
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **trace}
            with open(self.logs_dir / "self_correction.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass   # 日志失败绝不影响主流程

    # ---------- 总结：把结果行转成结论文本 ----------
    @staticmethod
    def _summarize(question: str, result: SqlResult) -> str:
        if result.row_count == 0:
            return "查询成功，但无匹配数据（0 行）。"
        # 单值：一句话结论
        if result.row_count == 1 and len(result.columns) == 1:
            return f"{question} → {result.rows[0][0]}"
        # 多行/多列：渲染小表格（最多 10 行）
        cols = result.columns or [f"col{i}" for i in range(len(result.rows[0]))]
        lines = ["| " + " | ".join(str(c) for c in cols) + " |",
                 "| " + " | ".join("---" for _ in cols) + " |"]
        for row in result.rows[:10]:
            lines.append("| " + " | ".join(str(c) for c in row) + " |")
        if result.row_count > 10:
            lines.append(f"\n（共 {result.row_count} 行，仅显示前 10 行）")
        return "\n".join(lines)

    def close(self) -> None:
        self.executor.close()

    def __enter__(self) -> "ChatBIEngine":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
