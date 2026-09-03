# PROJECT RESUME MATERIAL —— Document Specialist Agent（最终项目简历素材）

> 目标岗位：**后端开发工程师 · Agent / LLM 应用方向**（参考 `resume/后端开发工程师-初稿.html`）。
> 本文按"最终项目简历"组织素材，每个技术点带状态字段；**未实现能力只能作为最终设计中的能力，必须明确标注未完成**。

## 0. 红线（禁止虚构）

- 禁止编造：性能数据、用户数量、QPS、成功率、生产环境数据、未实现功能的实际效果。
- 允许：真实测试结果（如 79 个用例通过）、真实代码结构、真实设计能力。
- 规则：**简历中每个技术点必须能回指本文件的【当前状态】**；PLANNED 的表述不能写成 DONE。

---

## 1. 项目定位与摘要

**一句话定位**：基于 AIO Sandbox 二次开发的企业级 Agent 执行平台 / Document Specialist Agent——把用户任务变成"规划 → 沙箱内多步工具执行 → 产物落盘 → 全程可追踪"的 Agent 运行时。

**最终态摘要（完整项目完成并验证后可写）**：

> 独立设计与实现企业级 Agent 执行平台：两级状态机任务管理、Planner→Executor 多轮 Tool Calling 闭环、可插拔工具注册表、沙箱隔离执行、错误分类 + 指数退避重试、权限白名单、短/长期记忆、对象存储产物交付与评估观测体系；79 个离线单测 + 端到端文档处理 Demo 验证。

**当前真实摘要（现在就能写）**：

> 独立实现 Agent 运行时核心：任务/步骤两级状态机与线程安全存储抽象；Planner → Executor 多轮 tool-calling（工具结果回传、防死循环）；BaseTool 抽象 + 动态工具注册表（run_python/read_file/save_report）；沙箱防腐层（base64 二进制安全、输出归一化、OSS↔沙箱数据闭环）；工具层重试（6 类错误分类 + 指数退避抖动，8 字段观测事件）；FastAPI 异步任务 API；79 个全离线单测（6 秒跑完）+ 端到端 Demo。

**核心流程**：

```text
User → Orchestrator → Planner → Executor → Tool Registry → Tool → Sandbox
     → Result → Validator / Recovery → Storage → Final Response
```

---

## 2. 核心技术点素材

### 2.1 LLM API 接入与适配

【目标能力】统一 OpenAI 兼容客户端（模型/base_url/api_key 可配置），懒构造、可注入，Planner 与 Executor 共用；支持 `tools` / `tool_choice` / `response.usage` 统计。

【当前状态】**DONE（部分）**：`agent/llm_client.py` 已实现懒构造与注入（无 key 可组装，首次 chat 才校验）；`response.usage` 累加**未实现**（PLANNED）。

【面试前必须掌握的内容】
- `LLMClient.chat()` 的 kwargs 组装与 `_ensure_client()` 懒加载。
- 为什么懒构造：demo/API 启动不依赖 API key，测试用 fake。
- 回答"怎么统计 token"：`response.usage` 累加进 `task.metrics`（说明设计，未实现）。

【面试风险】若声称"已统计 token 消耗"会被追问细节露馅；正确说法：**设计已预留 `Task.metrics`，usage 累加是规划项**。

### 2.2 Tool Calling（多轮函数调用闭环）

【目标能力】LLM 决定调工具 → 执行 → 结果按 `role:"tool"` + `tool_call_id` 回传 → 再决策，直到无 tool_calls 或达到上限；防死循环、防御式参数解析。

【当前状态】**DONE**：`agent/executor.py` 实现完整闭环；`max_iterations=8`；失败步骤不中断；`_assistant_message` 归一化消息。

【面试前必须掌握的内容】
- 循环终止条件（无 tool_calls / 超上限抛 `MaxIterationsError`）。
- `message.content` 在有 tool_calls 时通常为 None，最终答案只在无 tool_calls 的一轮。
- 最容易漏的字段：`role:"tool"` 必须带 `tool_call_id`。
- 真实 Bug：真机 SDK 响应多一层 `.data`、`_normalize` 变量遮蔽（`4c7b04c` 修复，可讲完整排查过程）。

【面试风险】会被追问"工具结果太大怎么办/为什么不会死循环/一轮多个 tool_call 怎么处理"。当前已防死循环、顺序执行；截断/摘要与并行是 PLANNED，需如实说明。

### 2.3 Agent Workflow（Orchestrator / Planner / Executor）

【目标能力】Orchestrator 编排完整生命周期：创建 → 启动 → 规划 → 执行 → 校验 → 成功/失败；Planner 用 `create_plan` 强制结构化计划；Executor 多轮执行；plan 与步骤轨迹可对齐。

【当前状态】**DONE（核心）**：Planner/Executor/Orchestrator 全部实现并有测试。**PLANNED**：Result Validator、任务级 Recovery、plan_id 与 task.steps 映射。

【面试前必须掌握的内容】
- `run()` 与 `run_task()` 拆分的目的（异步提交 + 轮询）。
- 失败回滚只在非终态 `fail_task`，不覆盖已成功任务（`test_orchestrator.py` 有断言）。
- Plan 与 TaskStep 的关系：MVP 中 plan 是执行上下文、task.steps 是执行轨迹。

【面试风险】若把 Validator/Recovery 说成已实现会被追问。当前失败恢复仅"步骤失败交还 LLM + 工具层重试"；任务级恢复是 PLANNED。

### 2.4 Task Management（任务生命周期）

【目标能力】任务/步骤两级状态机、可替换存储（内存→Redis）、并发安全、序列化契约、全程可查询。

【当前状态】**DONE（内存版）**：状态机 + `TaskStore` ABC + `InMemoryTaskStore` + 双 RLock + 20 线程并发冒烟。**PLANNED**：Redis 版 TaskStore + 持久化队列。

【面试前必须掌握的内容】
- 状态机合法流转表（`CREATED→RUNNING→SUCCESS/FAILED`），非法流转抛 `TaskStateError`。
- `to_dict/from_dict` 是换存储和跨进程的前提。
- 内存版边界：进程重启丢任务——主动讲"TaskStore ABC 已预留，Redis 版是下一步"，这是加分项不是减分项。

【面试风险】被问"多进程怎么办"时不能含糊：明确内存版仅限本地/演示，生产需 Redis。

### 2.5 Sandbox（隔离执行层）

【目标能力】在隔离 Docker 容器中执行代码/命令；二进制安全传输；输出归一化；鉴权；数据进出闭环。

【当前状态】**DONE**：`SandboxClient` 防腐层 + `HermesHookEngine` pre/post hook。

【面试前必须掌握的内容】
- 二进制方案：`write_file(encoding="base64")` 写入，`download_file` 字节流读出（读 SDK 源码确认，非猜）。
- 四类 Jupyter 输出（stream/execute_result/display_data/error）归一化；成功判据是 `status`。
- API key 通过 `X-AIO-API-Key` header；`shlex.quote` 防注入删除。
- 真实 Bug：xlsx UTF-8 解码必炸（修复前）、真机 `.data` 解包、变量遮蔽。

【面试风险】被追问"沙箱里执行任意代码安全吗"——必须回答：沙箱是隔离边界，但**权限白名单层（PLANNED）**才是平台级控制；当前单测用 fake SDK，真机验证依赖 docker 环境。

### 2.6 Retry / Recovery

【目标能力】错误分类（瞬态 vs 确定性）、指数退避 + 抖动重试、8 字段观测事件、任务级恢复。

【当前状态】**DONE（工具层重试）**：`ErrorType` 6 类 + `RetryPolicy` 纯决策 + Executor `_invoke_tool` 集成。**PLANNED**：LLM 调用重试、任务级 Recovery。

【面试前必须掌握的内容】
- 核心判据："同一请求原样重发有多大可能成功"——只重试 TRANSIENT/TIMEOUT。
- 为什么放 Executor 层而不是工具内/Orchestrator：单点统一 + 有 task/tool/attempt 上下文。
- 退避公式：`base * factor^(attempt-1)`，抖动 ±50% 防 thundering herd，`max_delay` 封顶。
- 重试失败后错误交还 LLM 换方案，不直接判任务死刑。

【面试风险】被问"任务级恢复做了吗"——如实：工具层已做，任务级（重规划）是设计中的下一步。

### 2.7 Permission / Security（权限与安全）

【目标能力】工具/文件/命令白名单、敏感操作审批、拒绝审计、密钥管理。

【当前状态】**PLANNED**：最终设计能力，当前未实现。当前安全边界仅靠沙箱隔离 + API key + `shlex.quote`。

【面试前必须掌握的内容】
- 设计：Policy Engine 在 ToolRegistry 分发前决策；白名单优于黑名单；拒绝写审计日志。
- 必须主动声明：**该层未实现**，是规划中的核心模块。

【面试风险】**最高风险点**：一旦在简历中暗示"已实现权限控制"而实际没有，面试追问（"怎么防路径穿越""怎么审批危险命令"）会直接露馅。正确表述只能是"最终设计能力，规划中"。

### 2.8 Short-term Memory（短期记忆）

【目标能力】任务级上下文组装、工具大结果截断/摘要/OSS 引用、上下文窗口管理。

【当前状态】**PLANNED**：当前依赖消息历史直接回传，无截断/摘要。

【面试前必须掌握的内容】
- 为什么需要：消息越长越贵越慢，大结果会撑爆上下文。
- 设计：每次 LLM 调用前组装（系统提示 + 任务上下文 + 历史摘要 + 当前状态）；大结果降级为摘要或 OSS key 引用。

【面试风险】被问"工具输出 10MB 怎么办"——如实：当前会全文回传（有风险），截断/摘要是规划项；能说出设计即加分。

### 2.9 Long-term Memory（长期记忆）

【目标能力】历史任务摘要入库（关键词/标签 → 向量检索），Planner/Executor 检索注入，同类任务复用经验。

【当前状态】**PLANNED**：未实现。

【面试前必须掌握的内容】
- 写入时机（任务结束）、检索时机（规划/执行前）、脱敏存储。

【面试风险】只能作为设计能力讲，不能写已实现。

### 2.10 Artifact Storage（产物存储）

【目标能力】MinIO/S3 结果层：懒创建 bucket、presigned URL 交付、二进制安全、产物元数据。

【当前状态】**DONE**：`StorageManager`（懒创建、幂等、错误包装、presigned URL）+ `save_report` 工具 + pre/post 数据闭环。

【面试前必须掌握的内容】
- 为什么"执行在沙箱、成果在 OSS"：沙箱无状态随时可弃，产物必须显式落盘。
- 懒创建 + 404 白名单（`NoSuchBucket`/`NotFound`）的坑。
- presigned URL 的时效与只读语义。

【面试风险】被问"大文件怎么办"——当前 base64 +33% 体积；流式上传是 DEFERRED，如实说明即可。

### 2.11 Evaluation（评估体系）

【目标能力】成功率、耗时、token、重试率、失败原因分布；固定 Eval 集可复跑。

【当前状态】**PLANNED**：`Task.metrics` 字段已预留；`retry_events` 已落数据；完整评估未实现。

【面试前必须掌握的内容】
- 数据源已具备（metrics + retry_events + steps），缺的是聚合与报告。
- **红线**：任何成功率数字都必须是真实统计，当前没有，所以简历不写。

【面试风险】编造成功率/耗时数字是最严重红线；当前只能写"评估体系设计完成、指标采集已预留"。

### 2.12 Observability（可观测性）

【目标能力】任务/步骤轨迹、重试 8 字段事件、usage 统计、task_id 维度日志、可选 trace。

【当前状态】**DONE（部分）**：步骤轨迹 + `retry_events`（8 字段）已实现并有测试；事件落库与指标面板 PLANNED。

【面试前必须掌握的内容】
- 8 字段：task_id / tool_name / attempt / error_type / error_message / retry_reason / duration_ms / final_status。
- 观测设计如何服务排查："任务到哪一步挂了"。

【面试风险】无重大风险，但不要把"落库/面板"说成已实现。

### 2.13 工程与测试基础设施

【目标能力】依赖注入全链路可测、79 个离线单测、端到端 demo、设计文档沉淀。

【当前状态】**DONE**：13 文件 / 79 用例（2026-09-01 复核 79 passed in 5.92s）；`demo/run_demo.py` + `demo/retry_demo.py`；8 篇设计文档。

【面试前必须掌握的内容】
- fake LLM / fake SDK / fake S3 的注入方式（为什么测试不需要 Docker/网络/key）。
- 20 线程并发冒烟、重试四场景确定性 demo。
- 真实数字：11 次提交、2 天、60 文件约 4178 行。

【面试风险】避免把"端到端 demo"说成"生产环境验证"——它需要 docker + LLM key，是本地演示链路。

---

## 3. 简历条目（最终态版，全部转 DONE 后才可投递）

> 以下每条标注状态；进入最终简历前，`【规划中】` 项必须真实实现并验证。

**Document Specialist Agent —— 企业级 Agent 执行平台**（独立开发）

- 【已实现】将单轮 demo 脚本重构为具备任务生命周期、工具注册表、计划-执行循环的 Agent 运行时；设计任务/步骤两级状态机与线程安全存储抽象，非法流转显式报错，20 线程并发冒烟通过。
- 【已实现】实现 Planner→Executor 多轮 Tool Calling 闭环（`create_plan` 强制结构化计划、工具结果回传、`max_iterations` 防死循环、失败步骤不中断交还 LLM 决策）。
- 【已实现】构建 BaseTool 抽象 + 动态工具注册表（run_python / read_file / save_report），JSON Schema 工具自描述，新增工具不改 Executor。
- 【已实现】实现沙箱防腐层：base64 二进制安全传输（修复 xlsx 上传必炸）、Jupyter 四类输出归一化、OSS↔沙箱 pre/post 数据闭环与安全删除。
- 【已实现】实现工具层重试：6 类错误分类 + 指数退避（含抖动）纯决策策略，只重试瞬态故障；8 字段重试事件写入任务指标。
- 【已实现】交付 79 个全离线单测（fake LLM/SDK/S3，6 秒跑完）与端到端 Demo（CSV→OSS→沙箱→Agent→结果 URL）；修复真机验证发现的 SDK 响应结构差异与变量遮蔽 Bug。
- 【规划中】权限白名单层（工具/文件/命令）、短期记忆（上下文截断/摘要）、结果校验与任务级恢复、Redis 持久化任务存储、markitdown 文档解析（PDF/Excel/PPTX）、评估指标落库与长期记忆。

---

## 4. 简历条目（当前可投版，全部真实）

与 `resume/后端开发工程师-初稿.html` 中项目经历一致，即"当前真实摘要"+ 已实现条目；**不包含**任何 PLANNED 能力。

---

## 5. 证据清单（所有数字可核验）

| 证据 | 数值/位置 | 状态 |
| --- | --- | --- |
| 单元测试 | 79 用例 / 13 文件，离线，5.92s（2026-09-01 复核） | 已确认 |
| 提交记录 | 11 commits，2026-08-30~31 | 已确认 |
| 代码规模 | 60 文件 / 约 4178 行（不含 venv/.git） | 已确认 |
| 模块规模 | task 410 · agent 407 · sandbox 224 · tools 230 · retry 95 · storage 99 · api 66 | 已确认 |
| 工具 | 3 个（run_python/read_file/save_report） | 已确认 |
| 错误分类 | 6 类 ErrorType，2 类可重试 | 已确认 |
| 重试参数 | 默认 3 次、base 1s、factor 2、max 10s、抖动 ±50% | 已确认 |
| 观测事件 | 8 字段 retry_events | 已确认 |
| 并发 | 20 线程生命周期冒烟 | 已确认 |
| 端到端 Demo | 设计为真机链路（需 docker + LLM_API_KEY） | 环境依赖 |
| 文档 | 8 篇设计笔记（10 小节模板） | 已确认 |

---

## 6. 面试追问应对（重点：未实现部分怎么答）

**统一话术**：

> "这部分在我的最终项目设计里，当前实现完成了 X（具体到文件/测试），我计划在 Y 阶段补齐，设计上是 Z（说清楚方案）。我没有把未实现的能力写进已完成部分。"

**高频追问与一句话答案**：

| 追问 | 一句话答案 |
| --- | --- |
| 怎么防止 LLM 无限调工具？ | `max_iterations=8` 上限，超限抛错并标记任务失败。 |
| 工具报错都要重试吗？ | 不；只重试瞬态（网络/429/超时/冷启动），判断标准是"原样重发多大可能成功"。 |
| 重试放哪层？ | Executor 内层循环：单点统一策略 + 有 task/tool/attempt 上下文，工具保持无状态。 |
| 内存存储重启丢任务怎么办？ | TaskStore ABC 已预留，Redis 版是下一步；后台线程换队列。 |
| 沙箱里执行任意代码安全吗？ | 沙箱是隔离边界；平台级权限白名单（规划中）才是控制手段，当前已启用 API key 与安全删除。 |
| 工具输出太大怎么办？ | 设计是截断/摘要或 OSS key 引用（规划中）；当前会回传全文，这是已知风险点。 |
| 成功率多少？ | 当前没有生产统计数据，不编造；有 79 个离线单测作为质量证据。 |
| 项目是你一个人做的吗？ | 独立开发（仓库为个人项目，git 提交可查）；如实际有协作按真实边界回答。 |

---

## 7. 里程碑计划（笔试/面试等待期补齐顺序）

1. **P0-1 Permission/Security 白名单层**（简历安全性的关键补强）
2. **P0-2 Short-term Memory（截断/摘要）**（解决上下文风险的工程问题）
3. **P0-3 Result Validator + 任务级 Recovery**
4. **P0-4 Redis TaskStore + 队列**（把"内存版"升级为可讲的生产设计）
5. **P0-5 markitdown 文档解析**（贴合 Document Specialist 定位）
6. **P0-6 LLM 重试 + usage 统计**
7. **P0-7 Evaluation 指标落库**（之后才有真实成功率可写）
8. P1 增强项按剩余时间取舍；每项完成即更新本文件状态字段与证据。

> 每完成一项：代码合入 → 测试通过 → 更新 [PROJECT_PROGRESS.md](PROJECT_PROGRESS.md) 状态 → 更新本文件对应【当前状态】。
