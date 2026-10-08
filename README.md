# ChatBI · Olist 电商数仓自然语言问数 Agent

把已交付的 **Olist 电商 MySQL 数仓（9 表 / 155 万行）** 从"人写 SQL 出报表"升级为"用中文直接问数"：

> 自然语言提问 → RAG 检索口径与历史 SQL → LLM 生成 SQL → **只读执行闸** → 校验 →（自纠错 / 口径·空结果守卫）→ 返回数字、表格与结论。

**评估准确率：93.3~96.7%**（30 题留出集；LLM 生成非确定性，多次运行在此区间波动，详见 [`eval/report.md`](eval/report.md)）

技术栈：Python 3.12 · **LangGraph（编排状态图）** · vanna 0.7.9（NL2SQL RAG）· ChromaDB · DeepSeek-V3.2（经 SiliconFlow）· pymysql + MySQL 8.4 · pytest。**零 GPU、全云端 API**。

---

## 架构

```mermaid
flowchart TD
    U["用户中文问题"] --> ASK["ChatBIEngine.ask()"]
    ASK --> RET["① 检索：vanna RAG<br/>召回 DDL / 口径文档 / 历史SQL"]
    CH[("ChromaDB 向量库<br/>三路训练材料")] -. 相似度召回 .-> RET
    RET --> GEN["② 生成：DeepSeek-V3.2 续写 SQL"]
    GEN --> EXE["③ 执行：ReadOnlyExecutor 只读闸"]

    subgraph GUARD["执行闸 · 四道防线"]
        direction TB
        W["1 语句白名单：仅 SELECT/WITH<br/>拦 DML·DDL·多语句·INTO OUTFILE·LOAD_FILE"]
        W --> LM["2 强制 LIMIT：无则注入"]
        LM --> TO["3 超时：会话 MAX_EXECUTION_TIME + 连接 read_timeout"]
        TO --> NE["4 错误归一化 → SqlError(code/errno/stage)"]
    end
    EXE --> GUARD
    GUARD --> DB[("MySQL ecommerce<br/>chatbi_ro 只读账号")]

    DB --> CHK{"④ 校验：执行成功？"}
    CHK -- "否·可重试<br/>(SEMANTIC/SYNTAX/TIMEOUT)" --> SC["自纠错：回填错误+DDL<br/>→ LLM 重写 1 轮"]
    SC --> EXE
    CHK -- "否·不可重试<br/>(BLOCKED/PERMISSION)" --> FAIL["安全终止"]
    CHK -- "是" --> G2["守卫：空结果改写 / 口径校验重写"]
    G2 --> SUM["⑤ 总结：确定性格式化<br/>单值→句子 / 多行→表格"]
    SUM --> ANS["数字 / 表格 / 结论"]
```

**关键设计**：编排流程用 **LangGraph StateGraph** 显式表达（[`chatbi/graph.py`](chatbi/graph.py)：节点=生成/执行/自纠错/守卫/总结，条件边=失败→自纠错、违规→重写复跑、成功→总结，环由重试上限终止）；vanna 只负责"检索 + 生成"，**执行权交给自写的 `ReadOnlyExecutor`**（不用 `vanna.run_sql`）——即使用编排框架，执行安全闸仍是自有组件、可逐行讲；安全靠**架构双保险**（应用层白名单 + DB 只读账号），不靠 prompt（见 [`docs/DECISIONS.md`](docs/DECISIONS.md) D3/D6/D7）。

---

## 目录结构

```
chatbi/                     # 产品代码包
  graph.py                  # LangGraph StateGraph 编排（节点 + 条件路由）
  engine.py                 # 引擎：持有资源 + 被图节点复用的 helper + ask/run_sql 入口
  executor.py               # 只读 SQL 执行闸（四道防线）
  guards.py                 # 口径/空结果守卫规则（纯函数）
  comparator.py             # 结果行级规范化比对器
  knowledge.py              # 知识层单源：三路训练材料 + 向量库构建流程
  secrets.py                # 凭据/配置解析单源（.env 与 db_ro.env，均不入库）
  ui_helpers.py             # 前端纯函数（图表推断/格式化/徽标/时间线/CSV）
main.py                     # 端到端入口（CLI 问数）
app.py                      # Streamlit 网页前端（答案优先多轮对话）
assets/style.css            # 前端品牌样式
scripts/
  create_readonly_user.py   # 建 chatbi_ro 只读账号（root 密码交互输入）
  train.py                  # 构建向量库（复现第一步；实现在 chatbi/knowledge.py）
  gen_schema_doc.py         # 从库生成 docs/schema.md
  run_eval.py               # 评估跑分 → eval/report.md
  verify_eval.py            # 评估集锚点验证 → eval/verified_results.json
  accept_ui.py              # 前端自动化验证（AppTest）
  demo_self_correction.py   # 自纠错演示
  demo_guards.py            # 守卫演示
  archive/                  # 历史一次性脚本（冒烟/排障/早期训练，见其 README）
eval/
  questions.yaml            # 30 题留出评估集（标准 SQL + 口径标注）
  verified_results.json     # 标准 SQL 验证快照
  report.md                 # 准确率报告
tests/                      # pytest（60 项）
docs/
  schema.md                 # 9 表 DDL + 中文注释（从库生成）
  metrics.md                # 指标口径（唯一口径事实源）
  DECISIONS.md              # 工程决策与踩坑日志 D1–D7
  ARCHITECTURE.md            # 分层/职责/调用边界/状态机/依赖清单
```

---

## 复现步骤

### 0. 前置
- Python 3.12+ 与 [uv](https://github.com/astral-sh/uv)
- 一个已载入 **Olist 电商数据**的 MySQL 8.x 库 `ecommerce`（9 表，表结构见 [`docs/schema.md`](docs/schema.md)）。数据为公开 Olist (Brazilian E-Commerce) 数据集；注意本项目 ETL 已把官方数据集拼错的 `lenght` 修正为 `length`（见 DECISIONS D4）。
- 一个 SiliconFlow API Key（用于调 DeepSeek-V3.2）。

### 1. 安装依赖
```bash
cd <项目目录>
uv sync
```

### 2. 配置凭据（均不进 git）
```bash
cp .env.example .env          # 然后编辑 .env 填入：SILICONFLOW_API_KEY=<你的Key>
```
只读账号连接信息 `config/db_ro.env` 由下一步脚本自动生成，无需手写。

### 3. 建只读账号 chatbi_ro（仅 SELECT）
```bash
uv run python scripts/create_readonly_user.py
# 按提示在终端输入 MySQL root 密码（不回显、不经聊天、不落日志）
# → 随机 20 位密码，创建 chatbi_ro@localhost 仅授 SELECT ON ecommerce.*
# → 凭据写入 config/db_ro.env（已 gitignore）
```

### 4. 构建向量库（三路训练）
```bash
uv run python scripts/train.py
```

### 5. 端到端问数
```bash
uv run python main.py                          # 跑 3 个内置 demo 问题
uv run python main.py "2017年黑五的GMV是多少？"  # 或自定义问题
```

### 5b. 网页界面（Streamlit 壳 · 答案优先多轮对话）
```bash
uv run streamlit run app.py                    # 🪙 小数点 · 电商问数助手
```
界面为纯壳：只调用 `ChatBIEngine.ask()`（`st.cache_resource` 缓存引擎，多轮追问不重连 MySQL / 不重载 chroma）。
布局：示例 chips → 多轮聊天流；每条答案=徽标条（N 行·耗时·chatbi_ro 只读；自纠错/口径守卫命中仅触发时亮）→ 结论大字号 → 自动图表（plotly：单值指标卡 / 时序折线 / 类目横向条形，按结果形状推断）→ 数据表格 + CSV 导出 → SQL 折叠（可复制）→ LangGraph 编排时间线折叠。
品牌视觉与图表辅助在 `chatbi/ui_helpers.py` + `assets/style.css`；自动化验证脚本 `scripts/accept_ui.py`（AppTest，16 项）。
新增依赖：plotly 7.0.0（交互式图表；锁定于 uv.lock）。凭据仍只走 `.env` / `config/db_ro.env`（永不进 git）。

### 6. 评估跑分（产出准确率报告）
```bash
uv run python scripts/run_eval.py              # → 重写 eval/report.md
```

### 7. 跑测试
```bash
uv run pytest                                  # 60 passed
```

---

## 评估

- **评估集**：[`eval/questions.yaml`](eval/questions.yaml)，30 题留出集（单表聚合 8 / 多表关联 10 / 时序环比 6 / 排名对比 6），问法与训练问答对不逐字重合（测泛化非记忆）。
- **口径**：分母=30，分子=**执行成功且结果一致**（行级规范化比对：排序无关、数值 2 位容差；SQL 文本相似度不计）。
- **结果**：多次运行 **93.3~96.7%**；单表聚合 / 多表关联 / 排名对比多次满分，时序环比受 Q23 表示差异略低。
- 持续未过题（Q23）为**比对器保守假阴性**——生成用 `DATE_FORMAT '%Y-%m'`、标准用 `MONTH()`，数据完全一致仅表示不同；按严格口径如实记录，不做"魔法对齐"骗分（真实语义准确率 30/30）。
- 详见 [`eval/report.md`](eval/report.md)。

---

## 安全模型（只读双保险）

| 层 | 手段 | 作用 |
|---|---|---|
| 应用层 | `ReadOnlyExecutor` 语句白名单 + 强制 LIMIT + 超时 | 不触库即拒危险语句；防百万行回传；慢查询服务端掐断 |
| DB 层 | `chatbi_ro` 账号仅 `GRANT SELECT` | 最终硬闸，即使白名单被绕过也无法写库 |

LLM 生成的 SQL 存在幻觉风险（可能输出 DELETE/DROP）。**安全靠架构不靠 prompt**：两层各司其职，任一层失效另一层仍在。自纠错/守卫的重写产物也一律再过执行闸二次安检。详见 [`docs/DECISIONS.md`](docs/DECISIONS.md) D3、D6。

---

## 红线

- `.env`、`config/*.env`（含 API Key、DB 密码）**永不进 git**（`.gitignore` 首条）。
- 数据库一律用只读账号 `chatbi_ro` 连接。
- `chroma/`、`logs/` 为运行产物，已 gitignore，不入库。

## 关于开发方式

本项目开发过程使用 AI 编程工具辅助，全部代码经人工审核、理解并可逐行讲解。
