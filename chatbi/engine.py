"""P2.2 + P2.3：编排主循环——「检索 → 生成 → 执行 → 校验 →（自纠错）→ 总结」端到端链路。

这是"自写编排"五件套的核心：不依赖任何 Agent 框架，每一步都是可逐行讲的显式代码。
  - 检索 + 生成：复用 vanna（RAG——内部检索 DDL/口径/历史 SQL 示例，再让 LLM 续写 SQL）
  - 执行：P2.1 的 ReadOnlyExecutor（只读闸，四道防线），**不用 vanna 的 run_sql**——
          执行权交给我们自己的安全闸，这正是"自写编排"而非"调框架"的意义
  - 校验：检查 SqlResult.ok；失败时带结构化 SqlError(code/errno/stage)
  - 自纠错（P2.3）：校验失败且错误可重试时，把错误回填给 LLM 重写 1 轮、复跑，全程写 logs/ 留痕
  - 总结：把结果行转成结论文本（确定性格式化，不再调 LLM——省 token 且可解释）

预留扩展点：
  - P2.4 空结果/口径守卫：在「校验通过」后插入 0 行改写与口径检查
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
from chatbi.guards import check_caliber, is_empty_result

ROOT = Path(__file__).resolve().parent.parent

# 可触发「回喂重写」的错误类别：语义/语法错可由 LLM 修正，超时可让其缩小范围，其余 DB 错给一次机会。
# 不可重试：BLOCKED（白名单安全拒绝，重写=试图绕过，必须终止）、PERMISSION（只读账号设计如此）、
#           GENERATE_FAIL/EMPTY_SQL（生成阶段问题，不在 SQL 修复范畴）。
RETRIABLE_CODES = {"SEMANTIC", "SYNTAX", "TIMEOUT", "DB_ERROR"}


def is_retriable_error(code: str) -> bool:
    """该错误码是否值得回喂 LLM 重写一轮。纯函数，便于单测。"""
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
    """问数引擎：vanna 负责「检索+生成」，ReadOnlyExecutor 负责「执行」，本类负责编排 + 自纠错。"""

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
                f"`uv run python scripts/train_and_test.py`（或 scripts/p1_5_sampling.py）。"
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

    # ---------- 编排主循环 ----------
    def ask(self, question: str) -> Answer:
        # 步骤 1+2：检索 + 生成（vanna RAG 内部完成检索，再让 LLM 续写 SQL）
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):     # 屏蔽 vanna 的 prompt 噪声
                sql = self.vn.generate_sql(question=question)
        except Exception as e:                        # 生成阶段异常（网络/模型）
            return Answer(ok=False, question=question, stage="generate",
                          error=SqlError(code="GENERATE_FAIL", stage="generate",
                                         message=f"{type(e).__name__}: {str(e)[:200]}"))
        if not sql or not sql.strip():
            return Answer(ok=False, question=question, stage="generate",
                          error=SqlError(code="EMPTY_SQL", stage="generate", message="生成空 SQL"))

        # 步骤 3+4：执行 + 校验 +（失败时）自纠错 1 轮（P2.3）
        result, trace = self.execute_with_correction(question, sql)
        if not result.ok:
            return Answer(ok=False, question=question, sql=result.sql, result=result,
                          stage="execute", error=result.error, trace=trace)

        # 步骤 4.5：P2.4 守卫——空结果（0 行）改写重试 + 口径校验/重写
        result, guard_trace = self.apply_guards(question, result)

        # 步骤 5：总结（确定性格式化，不再调 LLM）
        healed = trace.get("final") == "ok_self_healed"
        summary = self._summarize(question, result)
        if healed:
            summary = "（经 1 轮自纠错后成功）\n" + summary
        if guard_trace.get("caliber_rewrite_sql"):
            summary = "（经口径守卫修正）\n" + summary
        return Answer(ok=True, question=question, sql=result.sql, result=result,
                      stage="ok", summary=summary, self_healed=healed, trace=trace,
                      caliber_violations=guard_trace.get("caliber_violations_final", []),
                      empty_retried=guard_trace.get("empty_trigger", False),
                      guard_trace=guard_trace)

    # ---------- P2.3：执行 + 自纠错 1 轮 ----------
    def execute_with_correction(self, question: str, sql: str) -> tuple[SqlResult, dict]:
        """执行 sql；失败且错误可重试 → 回喂错误让 LLM 重写 1 轮 → 复跑（仍过执行闸）。

        返回 (最终 SqlResult, trace)。trace 记录全过程并落 logs/self_correction.jsonl。
        公开方法：既供 ask() 调用，也供验收 demo 直接注入「人为构造的错误 SQL」。
        """
        trace: dict = {"question": question, "attempt0_sql": sql}

        r0 = self.executor.execute(sql)          # 第 1 次执行（过 P2.1 只读闸）
        if r0.ok:
            trace["final"] = "ok_attempt0"
            return r0, trace

        err = r0.error
        trace["attempt0_error"] = {"code": err.code, "errno": err.errno, "message": err.message}

        # 分流：不可重试的错误（BLOCKED/PERMISSION 等）直接终止，不浪费一次 LLM 调用
        if not is_retriable_error(err.code):
            trace["final"] = f"fail_non_retriable_{err.code}"
            self._log_trace(trace)
            return r0, trace

        # 自纠错：回填错误 + DDL → LLM 重写
        fixed = self._repair_sql(question, r0.sql or sql, err)
        trace["attempt1_sql"] = fixed
        if not fixed:
            trace["final"] = "fail_repair_empty"
            self._log_trace(trace)
            return r0, trace

        r1 = self.executor.execute(fixed)        # 复跑（重写产物同样过只读闸，二次安检）
        trace["attempt1_ok"] = r1.ok
        if r1.ok:
            trace["final"] = "ok_self_healed"
        else:
            trace["attempt1_error"] = {"code": r1.error.code, "errno": r1.error.errno,
                                       "message": r1.error.message}
            trace["final"] = "fail_after_repair"
        self._log_trace(trace)
        return r1, trace

    def _repair_sql(self, question: str, bad_sql: str, error: SqlError) -> str | None:
        """P2.3：把执行报错回喂 LLM 重写。是 _rewrite_sql 的错误专用包装。"""
        reason = f"在 MySQL 上执行报错，code={error.code}（errno={error.errno}）：{error.message}"
        return self._rewrite_sql(question, bad_sql, reason)

    def _rewrite_sql(self, question: str, sql: str, reason: str) -> str | None:
        """通用重写：把「当前 SQL + 需要修正的原因 + 相关 DDL」回喂 LLM，要一条修正后的 SELECT。

        P2.3（执行报错）与 P2.4（空结果/口径违规）共用此入口。失败返回 None。
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

    # ---------- P2.4：空结果守卫 + 口径守卫 ----------
    def apply_guards(self, question: str, result: SqlResult) -> tuple[SqlResult, dict]:
        """在执行成功后施加两道守卫，各最多改写 1 轮；返回 (可能更新后的 result, guard_trace)。

        - 空结果守卫：0 行 → 提示"过滤值可能不存在/条件过严"重写 1 轮；仍 0 行则接受为合法答案。
        - 口径守卫：按问题意图校验 SQL 是否命中 metrics.md 口径；违规则注入口径重写 1 轮并复检。
        正确的查询两道守卫都不触发 LLM（零额外成本）。
        """
        trace: dict = {}
        sql = result.sql

        # 守卫 1：空结果
        if is_empty_result(result):
            trace["empty_trigger"] = True
            hint = ("上一条 SQL 执行成功但返回 0 行。可能是过滤值不存在（拼写/语言/日期格式不符）"
                    "或条件过严。请核对口径、修正或放宽过滤条件，仍只输出一条 SELECT。")
            fixed = self._rewrite_sql(question, sql, hint)
            trace["empty_rewrite_sql"] = fixed
            if fixed:
                r2 = self.executor.execute(fixed)
                trace["empty_rewrite_ok"] = r2.ok
                if r2.ok:
                    trace["empty_rewrite_rows"] = r2.row_count
                    result, sql = r2, r2.sql

        # 守卫 2：口径
        violations = check_caliber(question, sql)
        trace["caliber_violations"] = violations
        if violations:
            trace["caliber_trigger"] = True
            hint = "生成 SQL 未命中项目口径：" + "；".join(violations) + "。请严格按口径修正，仍只输出一条 SELECT。"
            fixed = self._rewrite_sql(question, sql, hint)
            trace["caliber_rewrite_sql"] = fixed
            if fixed:
                r3 = self.executor.execute(fixed)
                if r3.ok:
                    result, sql = r3, r3.sql
            trace["caliber_violations_final"] = check_caliber(question, sql)
        else:
            trace["caliber_violations_final"] = []

        if trace.get("empty_trigger") or trace.get("caliber_trigger"):
            self._log_trace({"question": question, "trigger": "guards", "final": "guards_done", **trace})
        return result, trace

    def _log_trace(self, trace: dict) -> None:
        """把一次自纠错留痕追加到 logs/self_correction.jsonl（logs/ 已 gitignore，运行产物不入库）。"""
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
