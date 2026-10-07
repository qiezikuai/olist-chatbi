# ChatBI 架构说明（ARCHITECTURE）

> 基线：`2d92f56`（main，工作区干净）。本文档描述系统分层、目录职责、模块调用边界与依赖关系。
> 工程决策与取舍的依据见 `docs/DECISIONS.md`。

## 1. 五层模型

```
┌────────────────────────────────────────────────────────────┐
│ 入口层    main.py (CLI)          app.py (Streamlit Web)    │
│             └── ChatBIEngine.ask() ──┘                    │
├────────────────────────────────────────────────────────────┤
│ 编排层    chatbi/graph.py   LangGraph StateGraph           │
│   节点: generate → execute → guards → summarize            │
│   回环: execute ┄失败可重试┄→ self_correct ┄→ execute       │
│         guards ┄重写后复跑┄→ execute                        │
│   宿主: chatbi/engine.py（持有资源 + 可复用 helper）        │
├────────────────────────────────────────────────────────────┤
│ 生成层    vanna 0.7.9（RAG：检索 DDL/口径/历史QA + LLM 续写）│
│ 执行层    chatbi/executor.py ReadOnlyExecutor（唯一执行通道）│
├────────────────────────────────────────────────────────────┤
│ 数据/知识层  MySQL ecommerce（chatbi_ro 只读）+ ChromaDB    │
│ 评估层    eval/questions.yaml × chatbi/comparator.py ×      │
│           scripts/run_eval.py → eval/report.md             │
└────────────────────────────────────────────────────────────┘
```

关键设计原则（详见 DECISIONS D3/D6/D7）：

1. **执行权自有**：全链路（包括 vanna 内部）不使用 `vanna.run_sql`，SQL 只经 `ReadOnlyExecutor.execute()`——白名单/强制 LIMIT/超时/错误归一化四道防线先于数据库生效。
2. **不信任 LLM 输出**：自纠错与守卫的重写产物一律再过执行闸；BLOCKED/PERMISSION 不触发重写（重写=试图绕过安全闸）。
3. **双保险**：应用层白名单 + `chatbi_ro` 仅 SELECT 权限，任一层失效另一层仍在。
4. **评估独立性**：`eval/` 与 `chatbi/comparator.py` 只消费引擎输出，不参与运行时。

## 2. 目录职责

| 路径 | 职责 | 消费方 |
|---|---|---|
| `chatbi/executor.py` | 只读执行闸；`SqlError/SqlResult` 结构化返回；连接复用与断连重连 | engine/graph 节点、run_eval、verify_eval（间接）、tests |
| `chatbi/guards.py` | 口径规则 `check_caliber`（metrics.md 的可机检子集）+ `is_empty_result` | graph 的 guards 节点 |
| `chatbi/comparator.py` | 结果集行级规范化比对 `results_match`（排序无关/数值 2 位容差/datetime 归一） | run_eval（评估跑分） |
| `chatbi/engine.py` | `ChatBIEngine` 资源持有（vanna、OpenAI client、executor）+ helper（`_repair_sql/_rewrite_sql/_summarize/_log_trace`）+ `Answer` 契约；`ask()/run_sql()` 为对外入口 | main.py、app.py、scripts（demo/run_eval/accept_ui） |
| `chatbi/graph.py` | LangGraph `StateGraph`：`AgentState` + 5 节点 + 条件路由；节点为闭包复用 engine helper | 仅 engine（`__init__` 内延迟构建） |
| `chatbi/ui_helpers.py` | 前端纯函数：列名映射/格式化/图表推断/徽标/时间线/CSV | app.py、tests |
| `app.py` | Streamlit 壳（答案优先多轮对话）；`st.cache_resource` 缓存引擎 | 用户浏览器 |
| `main.py` | CLI：打印五阶段（①-⑤）跑 demo 或自定义问题 | 用户终端 |
| `eval/questions.yaml` | 30 题留出评估集（标准 SQL + 口径标注） | run_eval、verify_eval |
| `eval/verified_results.json` | 标准 SQL 实跑快照（锚点核对证据） | 人工比对/漂移检测 |
| `eval/report.md` | 跑分报告（由 run_eval 生成） | 阅读者 |
| `docs/schema.md` | 9 表 DDL+注释（gen_schema_doc 从库生成） | 训练第 1 路取数源、人工参考 |
| `docs/metrics.md` | 指标口径唯一事实源 | 训练第 2 路摘要来源（人工转写）、guards 规则依据 |
| `scripts/` | 建号/训练/评估/诊断/演示脚本（历史脚本与在用工具有混放，见 §3 过渡结构说明） | 终端 |
| `tests/` | 60 项 pytest（executor 25 / comparator 9 / ui 9 / guards 8 / self-correction 5 / engine 4） | CI/本地 |

## 3. 模块调用边界（import 图）

```
main.py ──> chatbi.engine ──> chatbi.executor
                │  └(延迟, __init__ 内)──> chatbi.graph ──> chatbi.executor, chatbi.guards,
                │                                            chatbi.engine(纯函数 is_retriable_error)
app.py ──> chatbi.engine + chatbi.ui_helpers ──> (无引擎依赖，只消费 Answer/SqlResult)
scripts/train.py ──> scripts/p1_5_sampling ──> scripts/train_and_test   ← 知识层暂存于 scripts 的过渡结构
scripts/run_eval.py ──> chatbi.engine + chatbi.comparator
scripts/verify_eval.py ──> 独立 pymysql 连接（不依赖 chatbi，锚点自行核对）
scripts/accept_ui.py ──> streamlit AppTest + chatbi.engine
```

边界规则（应然）：

- `chatbi/` 包不 import `scripts/` 任何内容。
- 入口层只依赖 `engine.ask()/run_sql()` 与 `Answer` 契约，不触碰 graph 内部。
- 评估层与运行时解耦：`comparator` 仅被 run_eval 消费。
已知边界现状（过渡结构，规划收敛）：训练正源暂存于 scripts（`train.py → p1_5_sampling → train_and_test` 素材链），计划收敛为包内单一模块；前端为演示壳，直接消费 `Answer` 契约字段（含 trace/guard_trace 留痕）。

## 4. 编排状态机

状态 `AgentState`（TypedDict, total=False）：`question / sql / result / error / guard_violations / final_answer` + 控制字段（`repairs / empty_retried / caliber_retried / guard_route / repair_sql / trace / guard_trace / self_healed / stage / ok / gen_sql0`）。

流转（LangGraph 条件边）：

| 起点 | 条件 | 去向 |
|---|---|---|
| START | state 已含 `sql`（run_sql 注入） | execute |
| START | 否则 | generate |
| generate | 生成异常/空 SQL | summarize（终态=失败） |
| generate | 成功 | execute |
| execute | `result.ok` | guards |
| execute | 失败且 `code∈RETRIABLE` 且 `repairs<1` | self_correct（→回 execute） |
| execute | 失败其余情形（BLOCKED/PERMISSION/预算用尽） | summarize |
| guards | 0 行且未空重试 / 口径违规且未口径重试 | execute（重写复跑） |
| guards | 干净 | summarize |

收敛保证：`repairs<1`、`empty_retried`、`caliber_retried` 各限一次，总重写上限 3 次；`recursion_limit=50` 兜底。留痕：`_log_trace` → `logs/self_correction.jsonl`（gitignore）。

## 5. 凭据与安全边界

- LLM Key：`chatbi/engine._read_llm_key()` 读 `.env`（`SILICONFLOW_API_KEY`）。
- DB 凭据：`ReadOnlyExecutor.from_env()` 读 `config/db_ro.env`（由 `scripts/create_readonly_user.py` 生成，getpass 交互、随机 20 位密码、仅 SELECT）。
- `.env`、`config/*.env`、`chroma/`、`logs/` 均在 `.gitignore`；任何代码不得打印凭据。

## 6. 依赖清单（运行时）

| 包 | 版本 | 用途 | 备注 |
|---|---|---|---|
| vanna | 0.7.9（钉死） | RAG+NL2SQL | 2.x 为不兼容重写（D1）；chromadb/openai 非其硬依赖，需显式声明 |
| langgraph | ≥1.2.12 | 编排 StateGraph | langchain-core 一并显式声明，为生态兼容预留（其官方传递依赖） |
| chromadb | ≥1.5.9 | 向量库 | 1.5.9 纯 Rust 免编译，Windows 无 MSVC 下的正确选择（D5） |
| openai | ≥3.13 | SiliconFlow 兼容端点客户端 | base_url 注入（D2） |
| pymysql | ≥1.2 | MySQL 驱动 | 只读账号 |
| streamlit / plotly | ≥1.64 / ≥7.0 | Web 壳与图表 | 函数内延迟 import |
| pyyaml | ≥6 | 评估集读写 | 被 run_eval/verify_eval 使用 |
| pytest | ≥9.1 | 测试 | `pythonpath=["."]` |
