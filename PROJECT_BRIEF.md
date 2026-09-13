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
| 已知修正 | `analyst` 账号实为 ALL PRIVILEGES（非只读）→ Day1 建 `chatbi_ro` SELECT-only 账号 + 应用层只读校验，双保险 | setup_mysql.py 源码核查 |
| 已知风险 | vanna 最新版已跳 2.0.2（大版本变更，与教程/旧 API 兼容性未验证）→ Day1 冒烟决定锁 2.0.2 或降锁 0.7.x 末版 | PyPI 实时查询 |

## 2. 技术选型（摘要，详见 docs/PLAN_v2.md 第 5 节）

pymysql 1.2.0（已装）+ vanna（版本 Day1 锁定）+ chroma（vanna 内置）+ pytest + streamlit(W3)。
全程云端 API（SiliconFlow/DeepSeek），本地零 GPU。兜底：若 vanna 冒烟失败或累计调试超 4h → 弃 vanna，自写 prompt+pymysql 直查的 NL2SQL（+8h，见 PLAN_v2 风险节）。

## 3. 决策日志

- 2026-09-13：接受考核官基准评审（19/30）结论"方向正确、范围失控"。原四周方案按 9 节模板重写为 docs/PLAN_v2.md：MVP 收缩为五件套（最小闭环/自写编排/自纠错1轮/30题评估集/README+git），多 Agent/MCP/Docker/Dify/OpenManus 全部移出关键路径。原 8 岗位 JD 覆盖表作废，简历 bullet 以 A 线标尺重写。
- 2026-09-13：环境四待办清零（VS Code✅ Watt 代理✅ SiliconFlow✅ Olist 连接✅）。

## 4. 待办（当前阻塞点）

1. 发起人将 docs/PLAN_v2.md 带给考核官评分 → 通过即开工 Phase 0
2. 开工 Day1：发起人在本机终端执行一次 root 密码操作建 chatbi_ro 账号（我来写脚本，密码不经过对话）
3. 开工 Day1：发起人把 SiliconFlow Key 填入 `.env`（我建好 `.env.example` 后通知，他亲自填）

## 5. 进度日志（每任务闭合后追加，附路径/hash）

- 2026-09-13 环境准备：见第 1 节证据列。本文件即首次落盘。
