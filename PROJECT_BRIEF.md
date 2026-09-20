# ChatBI 项目 · 共享内存（唯一事实源）

> 协议：智谱（执行）写入进度与选型；发起人带给考核官（WorkBuddy）审核；考核官只认本文件最新落盘内容。
> 一切"已完成"声称必须附文件路径或 git commit hash。修订须发起人确认。

## 0. 锁定参数（来自《审核标准清单 v1.0》，不得更改）

| 项 | 值 |
|---|---|
| 求职主线 | A 线：福建本地数据/实施/BI 岗（厦福泉）；基线简历 `D:\简历\邱泽凯_实施顾问_BI数据交付.docx` |
| 每日投入 | 2–3 小时（不确定，按低值 2h 对账） |
| 时间线 | 09-13 开工 → 09-20 中期检查 → 09-27 MVP 验收 → 10-11 完整版定稿 |
| 中断规则 | 舒华/顾诺面试每占 1 个全天，MVP 截止顺延 3 天，或当场砍范围（砍序见清单 3.4） |
| 红线 | API Key/.env 永不进 AI 上下文与 git；项目放 D 盘 |
| GitHub 仪式 | 每日先开 Watt Toolkit 加速（HTTP 代理 127.0.0.1:26561）再 push；代理已按作用域配好（仅 github.com） |

## 1. 环境状态（09-13 实测就绪，全部已落盘验证）

| 项 | 状态 | 证据 |
|---|---|---|
| VS Code | 1.137.0 已装，Python(含Pylance 2026.3.1)+中文包已配 | 本机安装日志；CLI `--version` 输出 |
| Git | 2.55.0；github.com 代理作用域配置完成（26561） | `git config --global --get-regexp github` 输出 |
| Python | 3.12.7 + pip 26.0.1 + uv 0.12.10 | 版本命令输出 |
| MySQL | MySQL84 运行中；`ecommerce` 库 9 表 155 万行实测可连 | analyst 账号实连验证，订单表 99,441 行与简历一致 |
| 数据 | 表名为短版（`olist_orders` 等，非 `*_dataset`）；库含 customers/orders/order_items/order_payments/order_reviews/products/sellers/geolocation/category_translation | `SHOW TABLES` + COUNT 实测 |
| SiliconFlow | 账号已注册，Key 由发起人持有；**Key 只由发起人亲自填入 .env，不进聊天窗口** | 发起人确认 |
| 已解决：vanna 版本 | **2.0.2 为彻底重写**（旧 API 全部移除，改为 Agent/Conversation 架构，`OpenAI_Chat`/`ChromaDB_VectorStore` 不复存在）→ 按预案降锁 **0.7.9**（经典 API 末版，2025-04-10）；组合类 8 个关键方法实测全部可用；另补装 openai、chromadb（vanna 未声明为硬依赖的坑） | 冒烟脚本同环境 import 实测 |
| 已知修正 | `analyst` 账号实为 ALL PRIVILEGES（非只读）→ Day1 建 `chatbi_ro` SELECT-only 账号 + 应用层只读校验，双保险 | setup_mysql.py 源码核查 |

## 2. 技术选型（摘要，详见 docs/PLAN_v2.md 第 5 节）

pymysql 1.2.0（已装）+ vanna（版本 Day1 锁定）+ chroma（vanna 内置）+ pytest + streamlit(W3)。
全程云端 API（SiliconFlow/DeepSeek），本地零 GPU。兜底：若 vanna 冒烟失败或累计调试超 4h → 弃 vanna，自写 prompt+pymysql 直查的 NL2SQL（+8h，见 PLAN_v2 风险节）。

## 3. 决策日志

- 2026-09-13：接受考核官基准评审（19/30）结论"方向正确、范围失控"。原四周方案按 9 节模板重写为 docs/PLAN_v2.md：MVP 收缩为五件套（最小闭环/自写编排/自纠错1轮/30题评估集/README+git），多 Agent/MCP/Docker/Dify/OpenManus 全部移出关键路径。原 8 岗位 JD 覆盖表作废，简历 bullet 以 A 线标尺重写。
- 2026-09-13：环境四待办清零（VS Code✅ Watt 代理✅ SiliconFlow✅ Olist 连接✅）。
- 2026-09-13：**方案 v2.0 通关（27/30），批准开工**。落实考核官三条件：root 密码终端交互输入、冒烟后 SELECT 核行数存档（已实测 9 表/1,550,922 行，待冒烟后经 chatbi_ro 复核）、当日任务可顺延 09-14 但硬节点（09-20/09-27/10-11）不动。
- 2026-09-13：vanna 版本决策——2.0.2 彻底重写不兼容旧生态，按预案降锁 0.7.9，组合类方法全验证（详见第 1 节）。
- 2026-09-13：对照发起人提出的"Agent 项目六大核心标准"（经核非行业规范、为面试指南体框架）逐条对账：①④核心满足、③满足（含"评估后不用多 Agent"的合理性论证）、⑤半满足、②⑥MVP 有意不做。结论：MVP 五件套不动；W3/W4 清单追加三个便宜补法——JSONL trace 日志(1.5h)、执行前 SQL 确认开关(1h)、FastAPI 薄层(2h，可选)，完整版预算 48h→52.5h（≤70h 红线不变）；企业级架构全家桶与强行多 Agent 明确拒绝，防守话术已备。详见 PLAN_v2 第 4/9 节。

## 4. 待办（当前阻塞点）

1. **发起人（阻塞 P1.1）**：SiliconFlow 充值 ≥10 元（预算 ≤30 元已批准）
2. **发起人（阻塞推送）**：打开 Watt Toolkit 加速
3. **智谱（上述完成后）**：重跑冒烟 → chatbi_ro 复核 9 表行数并存档口径 → 加 remote 首推 → 更新本文件

## 5. 进度日志（每任务闭合后追加，附路径/hash）

- 2026-09-13 ｜ 环境准备完成（证据见第 1 节）｜ commit `263d531`
- 2026-09-13 ｜ P0.1 完成 ｜ 仓库初始化+uv.lock 锁定（vanna==0.7.9/pymysql/python-dotenv/pytest/openai/chromadb）+ 只读账号脚本 + 冒烟脚本 ｜ commit `263d531`
- 2026-09-13 ｜ P0.2 脚本就绪待发起人执行 ｜ scripts/create_readonly_user.py（getpass 交互，密码不入对话）｜ commit `263d531`
- 2026-09-13 ｜ P1.1 冒烟脚本就绪待 Key ｜ scripts/smoke_test.py ｜ commit `263d531`
- 2026-09-13 ｜ P1.1 调试（2 轮）：①首跑超时→诊断脚本隔离出根因：**vanna 0.7.9 的 OpenAI_Chat 忽略 config.base_url**（读源码确认，请求误发 api.openai.com）→ 改为注入自建 OpenAI client（base_url+timeout90+retries）②修复后触达 SiliconFlow 但 **402 余额不足**（账户余额 0，vanna 长 prompt 预检被拒；诊断确认 Key 有效、V3.2 直调 0.9s 正常、免费模型亦需余额>0）→ **阻塞点：发起人充值 ≥10 元** ｜ scripts/smoke_test.py+diag_api.py ｜ commit `947142f`
- 2026-09-13 ｜ P0.2 完成（发起人终端执行，chatbi_ro 已建）；.env 已由发起人填写 ｜ 发起人口头确认
