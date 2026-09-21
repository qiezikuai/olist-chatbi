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

        # >>> P2.4 扩展点：空结果守卫（0 行→改写重试）+ 口径检查注入 <<<

        # 步骤 5：总结（确定性格式化，不再调 LLM）
        healed = trace.get("final") == "ok_self_healed"
        summary = self._summarize(question, result)
        if healed:
            summary = "（经 1 轮自纠错后成功）\n" + summary
        return Answer(ok=True, question=question, sql=result.sql, result=result,
                      stage="ok", summary=summary, self_healed=healed, trace=trace)

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
        """把失败 SQL + 错误信息 + 相关 DDL 回喂 LLM，要一条修正后的 SELECT。失败返回 None。"""
        ddl_hint = ""
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ddls = self.vn.get_related_ddl(question) or []
            ddl_hint = "\n".join(str(d) for d in ddls[:6])
        except Exception:
            ddl_hint = ""
        messages = [
            {"role": "system", "content":
                "你是 MySQL 专家。用户的一条 SQL 执行失败，请依据错误信息和表结构修正它。"
                "只输出一条修正后的、可执行的 SELECT 语句，不要解释、不要 markdown 围栏。"},
            {"role": "user", "content":
                f"业务问题：{question}\n"
                f"失败的 SQL：{bad_sql}\n"
                f"错误码：{error.code}（errno={error.errno}）\n"
                f"错误信息：{error.message}\n"
                f"相关表结构：\n{ddl_hint}\n"
                f"请输出修正后的 SELECT 语句："},
        ]
        try:
            resp = self._client.chat.completions.create(model=self.model, messages=messages)
            return _clean_sql(resp.choices[0].message.content)
        except Exception:
            return None

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
