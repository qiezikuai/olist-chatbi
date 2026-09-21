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

---

## D5. chroma HNSW "Nothing found on disk"（2026-09-20）

**现象**：P1.4 训练后试跑，固定模式炸——第 1 题能查，第 2 题起 `chromadb.InternalError: Error creating hnsw segment reader: Nothing found on disk`。

**误诊路径（值得记录）**：
1. 第一反应："chromadb 1.5.9 新 Rust 架构与 vanna 0.7.9（2025-04）不兼容"→ 尝试降级 0.5.9。
2. **降级失败**：0.5.9 依赖 `chroma-hnswlib`，需本地 C++ 编译（MSVC），Windows 无构建链，uv add 报构建失败——但 lock 保持 1.5.9。
3. `rm -rf chroma/` 干净重建后重跑：**3 题全过**——而此时 chromadb 仍是 1.5.9。

**真根因**：不是版本兼容，是**训练后立即查询的竞态 + 脏段**。首次运行训练写入后 HNSW 段未完全落盘即查询；重跑时旧脏段与新写入叠加导致段损坏。`rm -rf` 重建 + 训练后 `sleep(2)` + 重新实例化 MyVanna 再查询 → 稳定复现通过。冷启动（不训练、直接加载已有 chroma）实测正常。

**处置**：`train_and_test.py` 采用"训练 → sleep(2) → 重新实例化 → 查询"流程；chroma 目录纳入 .gitignore（运行产物不入库）；**正式训练前必须 rm -rf chroma/ 干净重建**（已写入 PLAN P1.4 验收标准）。

**沉淀**：
- 降级失败反而是好事：hnswlib 需要 C++ 工具链，而 1.5.9 纯 Rust 绑定零编译依赖——**在无 MSVC 的 Windows 上，新版本恰恰是正确选择**。"降级保稳"不是万能公式，要验证失败假设。
- 排障顺序应该是：先排除"脏状态"（rm 重建），再动依赖版本。本次我先动了版本，多花了一轮。
- 向量库的"训练"是写盘操作，批量写入后给落盘留时间窗口，这是所有嵌入式向量库的通病（faiss/chroma 同理）。

---

## D6. 只读执行闸的安全模型：白名单 + 只读账号双保险（2026-09-20）

**背景**：P2.1 要落地"只读执行"这道闸。NL2SQL 的 SQL 由 LLM 生成、存在幻觉——可能输出 DELETE/UPDATE/DROP，甚至 `SELECT ... INTO OUTFILE` 写文件、`LOAD_FILE()` 读服务器文件。

**决策 1：为什么"应用层白名单 + DB 只读账号"双层，而非只靠一层？**
- 只靠 prompt 写"请只生成 SELECT"：防护力≈0（D3 已记），LLM 可被绕过或自行幻觉。
- 只靠 DB 只读账号（`chatbi_ro` 仅 SELECT 权限）：能挡 DML/DDL，但把拒绝权全交给 DB——危险语句仍会触库、返回的是 DB 报错（如 1142），不够可控、不够可读。
- 双层价值：①**纵深防御**，任一层失效另一层仍在；②应用层白名单能**在不触库的前提下**拒绝危险语句，返回结构化 `BLOCKED`（stage=whitelist），比 DB 报错更可控、可解释、可回喂；③**职责分离**——白名单管"语句类型/结构"，DB 权限管"最终执行权"。

**决策 2：为什么白名单用手写扫描挖空字符串/注释，而非正则或 sqlparse？**
- 正则难处理引号/注释嵌套：`WHERE order_id='a;b'` 里的分号、`SELECT '-- x'` 里的横线会被误判成多语句/注释。
- sqlparse 类解析器：能解析但不必然"理解"安全语义，且引入额外依赖，MVP 阶段不划算。
- 手写扫描（约 40 行）把字符串字面量与注释挖空成"骨架"再判定：既避免上述误判，又能拦住 `SELECT 1; DROP TABLE`（去尾分号后仍含内部分号→判多语句拒绝）、`INTO OUTFILE`、`LOAD_FILE`。代码可逐行讲，契合"自写编排"。

**决策 3：为什么超时用服务端 `MAX_EXECUTION_TIME`，而非只靠客户端 `read_timeout`？**
- 客户端 `read_timeout`：到点丢弃连接，但服务端查询线程可能仍在跑（大表全扫），白占 DB 资源，且连接需重建。
- 服务端 `MAX_EXECUTION_TIME`（会话级/毫秒）：MySQL **主动中止**超时 SELECT（错误 3024），连接保持可用、资源即时释放；且仅对只读 SELECT 生效，正好契合本工具定位。
- 两者叠加：服务端掐断为主、客户端 `read_timeout` 兜底（防服务端未及时中止时的网络挂起）。实测 `SELECT SLEEP(10)` 在 `timeout_s=3` 下 <6s 被掐断（code=TIMEOUT）。

**决策 4：为什么 `execute()` 不抛异常、统一返回 `SqlResult`？**
- 编排层（P2.2）与自纠错（P2.3）需按错误**类别**决策：`SEMANTIC`（未知列/表）可回喂 LLM 重写、`TIMEOUT` 应缩小范围重试、`BLOCKED` 直接终止。异常控制流难以承载这种结构化分类。
- 归一化为 `SqlError(code/errno/stage)`，把"错误"变成可被程序消费的数据，是自纠错闭环的前提。

**沉淀**：
- **安全靠架构不靠 prompt**：权限层（DB 只读账号）是硬闸，应用层白名单是可控的前置闸 + 清晰错误源，两层各司其职。
- **"不触库就拒绝" 优于 "触库后被 DB 拒"**：省一次往返、错误更可读、不给危险语句任何执行机会。
- **超时要设在"离数据最近的一侧"**：服务端中止查询 > 客户端断开连接。
