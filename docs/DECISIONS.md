# ChatBI 工程决策与踩坑日志

> 项目推进过程中的技术决策与问题处置记录。
> **原则**：每条 = 现象 → 根因 → 处置 → 沉淀。面试时以本文件为底稿讲自己的决策，比背模板可信。

---

## D1. vanna 版本降锁（2026-09-13）

**现象**：PyPI 最新 2.0.2，0.7.x 系列停在 0.7.9（2025-04），教程/文档/社区问答全部基于 0.7.x 经典 API。

**调查**：
- `vanna 2.0.2` 顶层暴露 `Agent/Conversation/WorkflowHandler`，经典 `OpenAI_Chat` / `ChromaDB_VectorStore` / `vanna.vanna` 全部不存在——彻底重写。
- `vanna 0.7.9` 组合类（`ChromaDB_VectorStore + OpenAI_Chat`）8 个关键方法（connect_to_mysql / train / generate_sql / ask / run_sql 等）全部可导入。
- vanna 未将 openai/chromadb 声明为硬依赖，需手动补装。

**决策**：降锁 0.7.9（经典 API 末版），锁定 `uv.lock`；vanna 2.0 重新成熟后再评估升级。

**预案**：若 0.7.9 累计调试超 4h → 弃 vanna，自写 prompt + pymysql 直查的 NL2SQL（+8h，PLAN 已有）。

**沉淀**：
- 任何半维护库 = 锁版本 + 写死兜底触发条件，不口头说"到时候再看"。
- 面试时这是"技术取舍"的活例证：读 release notes → 评估文档/社区/兼容性 → 做取舍，不是无脑 pip install 最新版。

---

## D2. base_url 被吞，请求发到 OpenAI（2026-09-13）

**现象**：首次冒烟 LLM 请求超时（默认 10s 无响应）。

**诊断**：
- 绕过 vanna 直调 SiliconFlow API，0.9s 返回正常 → **LLM 侧没问题**。
- 读 `vanna/openai/openai_chat.py` 源码：构造函数只读 `config["api_key"]` 调 `OpenAI(api_key=...)`，**完全不理会 config["base_url"]** → 请求发到 `api.openai.com`（国内不通）。

**处置**：vanna 支持另一种入口——外部传入 `client` 参数。自行构造带 `base_url + timeout(90) + max_retries(2)` 的 OpenAI client 注入进去，绕过 config 解析缺陷。

**沉淀**：
- 依赖库的行为以**源码为准**，以 README/config 样例为辅。0.7.9 是成熟版本但仍有坑，说明"版本锁了≠不用读源码"。
- 调试方法论：**变量隔离**——哪一段通了、哪一段没通，把"框架层/网络层/模型层"三段逐段验证，比"改 config 试试、换个模型试试"高效一个量级。这段经历后来被记入 `gen_schema_doc.py` 的自检逻辑（DDL 列与注释交叉比对）——**每次踩坑都要沉淀成机制，别只靠记忆**。

---

## D3. 只读账号实证（2026-09-13）

**现象**：`analyst` 账号（旧项目创建）实测为 `ALL PRIVILEGES`，非只读。

**风险**：LLM 生成 SQL 存在幻觉（可能输出 DELETE/UPDATE/DROP）；若执行账号权限过大，一次幻觉=一次数据事故。

**处置**：新建 `chatbi_ro@localhost`，仅 `GRANT SELECT ON ecommerce.*`，密码随机 20 位，凭据写入 `config/db_ro.env`（已 gitignore）。实测 `DELETE` 被拒（错误码 1142）。应用层再叠一层 SQL 语句白名单校验（P2.1），双保险。

**沉淀**：
- **安全靠架构不靠 Prompt**：在 prompt 里写"请只生成 SELECT"的防护力≈0；权限层是硬闸。
- `analyst` 的历史授权说明一个通用原则：项目间账号不共用，新项目从零建最小权限账号，别复用"看起来差不多"的旧账号。

---

## D4. ETL 列名修正（2026-09-20）

**现象**：`gen_schema_doc.py` 自检报"注释了但 DDL 中不存在的列 `product_name_lenght` / `product_description_lenght`"。

**根因**：Olist 官方 Kaggle 数据集列名拼写错误（`lenght`），当年 ETL 入库时已修正为 `length`，但本次注释字典照抄了公开数据集文档。

**处置**：修正字典键名为 `length`，自检 0 缺口。

**沉淀**：**训练材料以你的库为准**，不盲信公开数据集文档。schema.md 生成器从库拉 DDL 就是为这事设计的——文档跟着库走，不跟着外部文档走。
