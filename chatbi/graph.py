"""LangGraph 编排层——把 P2.2 的自写主循环用 StateGraph 显式表达。

原 imperative 循环（检索→生成→执行→自纠错→守卫→总结）重写为状态图：
  节点 = 各步骤；条件边 = 失败/违规/成功的路由。

四层不变（本文件只动「编排层」）：
  ① 数据层 MySQL+chatbi_ro ② 知识层 三路训练+chroma ③ 生成层 vanna ④ 评估层 eval/ ——均不动。
  executor.py（只读执行闸）、guards.py（守卫）、comparator.py（比对器）原样复用、包装成节点，不重写。

StateGraph 映射（详见 docs/DECISIONS.md D7）：
  START ─(有预置 sql?)→ execute | generate
  generate ─(出错?)→ summarize | execute
  execute  ─(成功→guards / 可重试错→self_correct / 不可重试或已修过→summarize)
  self_correct ─(重写成功→execute / 失败→summarize)
  guards   ─(空结果或口径违规且未重写过→execute / 否则→summarize)
  summarize → END
"""
from __future__ import annotations

import contextlib
import io
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

from chatbi.executor import SqlError
from chatbi.guards import check_caliber, is_empty_result
# engine 在 __init__ 内「延迟」import build_graph，故此处的 module-level import 不构成循环
from chatbi.engine import is_retriable_error

# 守卫重写提示（原 engine.apply_guards 内的文案，迁移到此）
EMPTY_HINT = ("上一条 SQL 执行成功但返回 0 行。可能是过滤值不存在（拼写/语言/日期格式不符）"
              "或条件过严。请核对口径、修正或放宽过滤条件，仍只输出一条 SELECT。")


def _caliber_hint(violations: list[str]) -> str:
    return "生成 SQL 未命中项目口径：" + "；".join(violations) + "。请严格按口径修正，仍只输出一条 SELECT。"


class AgentState(TypedDict, total=False):
    """编排状态。total=False：各节点增量返回、由 LangGraph 合并。"""
    # —— 任务建议的核心字段 ——
    question: str
    sql: str                    # 当前待执行/已执行的 SQL（重写后被覆盖）
    result: object              # SqlResult
    error: object               # SqlError | None
    guard_violations: list      # 最终仍违反的口径（空=命中）
    final_answer: str           # 总结文本
    # —— 控制/留痕字段 ——
    gen_sql0: str               # 首次生成的 SQL
    ok: bool
    stage: str                  # generate / execute / ok
    repairs: int                # 自纠错轮数（≤1）
    repair_sql: str             # 自纠错重写后的 SQL（有则路由回 execute）
    empty_retried: bool
    caliber_retried: bool
    guard_route: str            # guards 节点决定的下一跳：execute / summarize
    self_healed: bool
    guard_trace: dict
    trace: dict


def build_graph(engine):
    """构建并编译编排状态图。节点为闭包，复用 engine 的资源与 helper（不重写组件）。"""

    # ---------- 节点 ----------
    def generate(state: AgentState) -> dict:
        """① 检索 + ② 生成：vanna RAG 检索 DDL/口径/示例并让 LLM 续写 SQL。"""
        q = state["question"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):   # 屏蔽 vanna prompt 噪声
                sql = engine.vn.generate_sql(question=q)
        except Exception as e:
            return {"ok": False, "stage": "generate",
                    "error": SqlError(code="GENERATE_FAIL", stage="generate",
                                      message=f"{type(e).__name__}: {str(e)[:200]}")}
        if not sql or not sql.strip():
            return {"ok": False, "stage": "generate",
                    "error": SqlError(code="EMPTY_SQL", stage="generate", message="生成空 SQL")}
        return {"sql": sql, "gen_sql0": sql, "error": None}

    def execute(state: AgentState) -> dict:
        """③ 执行：P2.1 只读执行闸（白名单/LIMIT/超时/错误归一化）。"""
        sql = state["sql"]
        result = engine.executor.execute(sql)
        return {"result": result, "sql": result.sql or sql,
                "error": None if result.ok else result.error,
                "stage": "ok" if result.ok else "execute"}

    def self_correct(state: AgentState) -> dict:
        """P2.3 自纠错：把错误回填给 LLM 重写 1 轮（重写产物仍会再过 execute 闸）。"""
        err = state.get("error")
        bad_sql = state.get("sql")
        fixed = engine._repair_sql(state["question"], bad_sql, err)
        trace = {"trigger": "self_correct", "attempt0_sql": bad_sql,
                 "attempt0_error": ({"code": err.code, "errno": err.errno, "message": err.message}
                                    if err is not None else None),
                 "attempt1_sql": fixed, "final": "ok_self_healed" if fixed else "fail_repair_empty"}
        engine._log_trace({"question": state["question"], **trace})
        updates = {"repairs": state.get("repairs", 0) + 1, "trace": trace}
        if fixed:
            updates["sql"] = fixed
            updates["repair_sql"] = fixed
        return updates

    def guards(state: AgentState) -> dict:
        """P2.4 守卫：空结果改写 + 口径校验/重写（各最多 1 轮，复用 guards.py 规则）。"""
        q = state["question"]
        result = state["result"]
        sql = state["sql"]
        gt = dict(state.get("guard_trace", {}))

        # 空结果守卫
        if is_empty_result(result) and not state.get("empty_retried"):
            fixed = engine._rewrite_sql(q, sql, EMPTY_HINT)
            gt["empty_trigger"] = True
            gt["empty_rewrite_sql"] = fixed
            engine._log_trace({"question": q, "trigger": "guards", "final": "guards_done", **gt})
            if fixed:
                return {"empty_retried": True, "sql": fixed, "guard_trace": gt, "guard_route": "execute"}
            return {"empty_retried": True, "guard_trace": gt, "guard_route": "summarize"}

        # 口径守卫
        violations = check_caliber(q, sql)
        if violations and not state.get("caliber_retried"):
            fixed = engine._rewrite_sql(q, sql, _caliber_hint(violations))
            gt["caliber_trigger"] = True
            gt["caliber_violations"] = violations
            gt["caliber_rewrite_sql"] = fixed
            engine._log_trace({"question": q, "trigger": "guards", "final": "guards_done", **gt})
            if fixed:
                return {"caliber_retried": True, "guard_violations": violations,
                        "sql": fixed, "guard_trace": gt, "guard_route": "execute"}
            return {"caliber_retried": True, "guard_violations": violations,
                    "guard_trace": gt, "guard_route": "summarize"}

        # 干净：在最终 SQL 上复检口径
        return {"guard_violations": check_caliber(q, sql), "guard_trace": gt, "guard_route": "summarize"}

    def summarize(state: AgentState) -> dict:
        """⑤ 总结：确定性格式化（单值→句子 / 多行→表格），不再调 LLM。"""
        result = state.get("result")
        if result is None or not result.ok:
            return {"ok": False, "stage": state.get("stage", "execute"), "final_answer": ""}
        summary = engine._summarize(state["question"], result)
        healed = state.get("repairs", 0) > 0
        if healed:
            summary = "（经 1 轮自纠错后成功）\n" + summary
        if state.get("caliber_retried"):
            summary = "（经口径守卫修正）\n" + summary
        return {"ok": True, "stage": "ok", "final_answer": summary, "self_healed": healed}

    # ---------- 条件路由 ----------
    def route_start(state: AgentState) -> str:
        return "execute" if state.get("sql") else "generate"   # run_sql 预置 sql → 跳过生成

    def route_after_generate(state: AgentState) -> str:
        return "summarize" if state.get("error") else "execute"

    def route_after_execute(state: AgentState) -> str:
        r = state.get("result")
        if r is None:
            return "summarize"
        if r.ok:
            return "guards"
        err = r.error
        if err is not None and is_retriable_error(err.code) and state.get("repairs", 0) < 1:
            return "self_correct"
        return "summarize"

    def route_after_self_correct(state: AgentState) -> str:
        return "execute" if state.get("repair_sql") else "summarize"

    def route_after_guards(state: AgentState) -> str:
        return state.get("guard_route", "summarize")

    # ---------- 组装 ----------
    g = StateGraph(AgentState)
    g.add_node("generate", generate)
    g.add_node("execute", execute)
    g.add_node("self_correct", self_correct)
    g.add_node("guards", guards)
    g.add_node("summarize", summarize)

    g.add_conditional_edges(START, route_start, {"generate": "generate", "execute": "execute"})
    g.add_conditional_edges("generate", route_after_generate, {"execute": "execute", "summarize": "summarize"})
    g.add_conditional_edges("execute", route_after_execute,
                            {"guards": "guards", "self_correct": "self_correct", "summarize": "summarize"})
    g.add_conditional_edges("self_correct", route_after_self_correct,
                            {"execute": "execute", "summarize": "summarize"})
    g.add_conditional_edges("guards", route_after_guards, {"execute": "execute", "summarize": "summarize"})
    g.add_edge("summarize", END)

    return g.compile()
