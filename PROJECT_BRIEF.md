# ChatBI 项目 · 共享内存（唯一事实源）

> 协议：智谱（执行）写入进度与选型；发起人带给考核官（WorkBuddy）审核；考核官只认本文件最新落盘内容。
> 一切"已完成"声称必须附文件路径或 git commit hash。修订须发起人确认。

## 0. 锁定参数（来自《审核标准清单 v1.0》，不得更改）

| 项 | 值 |
|---|---|
| 求职主线 | A 线：福建本地数据/实施/BI 岗（厦福泉）；基线简历 `D:\简历\邱泽凯_实施顾问_BI数据交付.docx` |
| 每日投入 | 2–3 小时（不确定，按低值 2h 对账） |
| 时间线 | **v2.1（发起人 09-20 拍板）**：11 月实习前完成完整版+简历包装；唯一方向标=**10 月中前 MVP 五件套闭环+30 题评估准确率数字**（软目标，越快越从容）；无每日排期，任务队列弹性推进（见 PLAN_v2 第 9 节） |
| 中断规则 | 弹性推进下面试日=当日不推进、方向标自然后移；方向标承压时才启用砍序（PLAN_v2 第 6 节） |
| 推进纪律 | 每任务 ≤1.5h 当日闭合、进度日志附 hash；发起人对"需手工办理"清单当日响应或明示"今天不推"（09-20 承诺）；断档恢复一律以 brief 最新落盘为准 |
| 红线 | API Key/.env 永不进 AI 上下文与 git；项目放 D 盘 |
| GitHub 仪式 | **修正（实测）**：Watt Toolkit 运行于 DNS 驱动拦截模式（26561 端口仅 PAC/System 代理模式开启，DNS 模式下不开）→ git **无需代理配置**，开加速后直连即可；原 scoped 代理配置已移除。仪式不变：每日先开 Watt 加速再 push | ls-remote 实测 exit=0；首推成功 |

## 1. 环境状态（09-13 实测就绪，全部已落盘验证）

| 项 | 状态 | 证据 |
|---|---|---|
| VS Code | 1.137.0 已装，Python(含Pylance 2026.3.1)+中文包已配 | 本机安装日志；CLI `--version` 输出 |
| Git | 2.55.0；github.com 代理作用域配置完成（26561） | `git config --global --get-regexp github` 输出 |
| Python | 3.12.7 + pip 26.0.1 + uv 0.12.10 | 版本命令输出 |
| MySQL | MySQL84 运行中；`ecommerce` 库 **9 表 1,550,922 行**（2026-09-13 经 `chatbi_ro` 实测存档：customers 99,441 / geolocation 1,000,163 / order_items 112,650 / order_payments 103,886 / order_reviews 99,224 / orders 99,441 / category_translation 71 / products 32,951 / sellers 3,095；订单表 99,441 与简历口径一致）；`chatbi_ro` 只读已实证（DELETE 被拒，错误码 1142） | pymysql 直连 COUNT 输出 |
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
- 2026-09-20：**发起人拍板时间线重置（PLAN v2.0→v2.1）**：11 月实习为硬边界，取消 09-27/10-11 里程碑与每日排期表；唯一方向标=10 月中前 MVP 闭环+30 题准确率数字（软目标）；明确"时间变宽≠范围膨胀"——MVP 出数字前 W3/W4 加法一律不启动。同日发起人承诺：对手工办理清单当日响应或明示不推（针对 09-13→09-20 的静默断档，工期内无代码推进，日志如实记录）。

## 4. 待办（当前阻塞点）

无阻塞、无手工项。**队列指针：▶ P1.4**（10 组问答对训练+试跑，≤1.5h；训练前先重置 chroma/ 目录清掉冒烟期的重复数据）。完整队列见 PLAN_v2 第 9 节。

## 5. 进度日志（每任务闭合后追加，附路径/hash）

- 2026-09-13 ｜ **P1.1 完成 ✅ 冒烟通过** ｜ 链路：chatbi_ro 连 MySQL → chroma 训练(1 DDL+1 QA) → SiliconFlow/DeepSeek-V3.2 生成 SQL（285.5 tokens）→ 执行返回 99,441（与简历口径一致）｜ smoke_test.py 输出存档于本条
- 2026-09-13 ｜ 考核官条件②落实：9 表行数经 chatbi_ro 复核并存档（见第 1 节），合计 1,550,922 行；只读权限实证（DELETE 被拒 1142）
- 2026-09-20 ｜ **如实记录：09-13→09-20 七天无代码推进（发起人断档）**；今日完成时间线重置（PLAN v2.1）并恢复推进。当前点位不变：P1.1 ✅ → 下一步 P1.2 ｜ commit 见本条 hash
- 2026-09-20 ｜ **P1.2 完成 ✅** ｜ scripts/gen_schema_doc.py 从库拉取真实 DDL+行数合并中文注释生成 docs/schema.md（9 表 267 行，注释缺口 0）；自检抓到真实差异：本项目 ETL 已修正官方数据集的 `lenght` 拼写错误（列名 length），注释字典已对齐 ｜ commit 见本条 hash
- 2026-09-20 ｜ **P1.3 完成 ✅** ｜ docs/metrics.md：全局约定 7 条（时间/范围/金额/客户/地区/类目/行数陷阱）+ GMV/复购率/客单价三口径定义 + 锚点 SQL 已对库验证（96,096 人、复购 2,997=3.12%、送达 96,478）+ 问法→口径映射表（评估集命题依据）｜ commit 见本条 hash

- 2026-09-13 ｜ 环境准备完成（证据见第 1 节）｜ commit `263d531`
- 2026-09-13 ｜ P0.1 完成 ｜ 仓库初始化+uv.lock 锁定（vanna==0.7.9/pymysql/python-dotenv/pytest/openai/chromadb）+ 只读账号脚本 + 冒烟脚本 ｜ commit `263d531`
- 2026-09-13 ｜ P0.2 脚本就绪待发起人执行 ｜ scripts/create_readonly_user.py（getpass 交互，密码不入对话）｜ commit `263d531`
- 2026-09-13 ｜ P1.1 冒烟脚本就绪待 Key ｜ scripts/smoke_test.py ｜ commit `263d531`
- 2026-09-13 ｜ P1.1 调试（2 轮）：①首跑超时→诊断脚本隔离出根因：**vanna 0.7.9 的 OpenAI_Chat 忽略 config.base_url**（读源码确认，请求误发 api.openai.com）→ 改为注入自建 OpenAI client（base_url+timeout90+retries）②修复后触达 SiliconFlow 但 **402 余额不足**（账户余额 0，vanna 长 prompt 预检被拒；诊断确认 Key 有效、V3.2 直调 0.9s 正常、免费模型亦需余额>0）→ **阻塞点：发起人充值 ≥10 元** ｜ scripts/smoke_test.py+diag_api.py ｜ commit `947142f`
- 2026-09-13 ｜ P0.2 完成（发起人终端执行，chatbi_ro 已建）；.env 已由发起人填写 ｜ 发起人口头确认
- 2026-09-13 ｜ GitHub 首推完成：仓库 https://github.com/qiezikuai/olist-chatbi 上线，main 分支含全部 6 commits；确认 Watt 为 DNS 驱动模式、git 直连免代理（代理配置已移除）｜ push 输出 + brief 本条
