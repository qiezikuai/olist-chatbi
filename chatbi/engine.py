"""P2.2：编排主循环——把「检索 → 生成 → 执行 → 校验 → 总结」串成一条端到端链路。

这是"自写编排"五件套的核心：不依赖任何 Agent 框架，每一步都是可逐行讲的显式代码。
  - 检索 + 生成：复用 vanna（RAG——内部检索 DDL/口径/历史 SQL 示例，再让 LLM 续写 SQL）
  - 执行：P2.1 的 ReadOnlyExecutor（只读闸，四道防线），**不用 vanna 的 run_sql**——
          执行权交给我们自己的安全闸，这正是"自写编排"而非"调框架"的意义
  - 校验：检查 SqlResult.ok；失败时带结构化 SqlError(code/errno/stage)
  - 总结：把结果行转成结论文本（确定性格式化，不再调 LLM——省 token 且可解释）

预留扩展点（后续任务在此插桩，不重构主循环）：
  - P2.3 自纠错 1 轮：在「校验失败」处按 error.code 决定是否回喂 LLM 重写
  - P2.4 空结果/口径守卫：在「校验通过」后插入 0 行改写与口径检查
"""
from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass
from pathlib import Path

from chatbi.executor import ReadOnlyExecutor, SqlError, SqlResult

ROOT = Path(__file__).resolve().parent.parent


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
    sql: str = ""                          # 实际执行的 SQL（已加 LIMIT）
    summary: str = ""                      # 结论文本
    result: SqlResult | None = None        # 执行结果（rows/columns/row_count）
    stage: str = ""                        # generate / execute / ok
    error: SqlError | None = None


class ChatBIEngine:
    """问数引擎：vanna 负责「检索+生成」，ReadOnlyExecutor 负责「执行」，本类负责编排。"""

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
        client = OpenAI(api_key=key, base_url="https://api.siliconflow.cn/v1",
                        timeout=90, max_retries=2)
        # 只用于「生成 SQL」，不调 connect_to_mysql / run_sql —— 执行走我们自己的只读闸
        self.vn = _MyVanna(client, {"model": model, "path": str(chroma_dir), "language": "中文"})
        self.executor = ReadOnlyExecutor.from_env(timeout_s=timeout_s, max_rows=max_rows)

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

        # 步骤 3：执行（P2.1 只读闸——白名单/LIMIT/超时/错误归一化都在里面）
        result = self.executor.execute(sql)

        # 步骤 4：校验
        if not result.ok:
            # >>> P2.3 扩展点：按 result.error.code 决定是否把错误回喂 LLM 重写 1 轮 <<<
            #     SEMANTIC/SYNTAX → 可重写；TIMEOUT → 缩小范围重试；BLOCKED → 直接终止
            return Answer(ok=False, question=question, sql=result.sql, result=result,
                          stage="execute", error=result.error)

        # >>> P2.4 扩展点：空结果守卫（0 行→改写重试）+ 口径检查注入 <<<

        # 步骤 5：总结（确定性格式化，不再调 LLM）
        summary = self._summarize(question, result)
        return Answer(ok=True, question=question, sql=result.sql, result=result,
                      stage="ok", summary=summary)

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
