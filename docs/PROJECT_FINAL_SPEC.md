# PROJECT FINAL SPEC —— 企业级 Agent Execution Platform / Document Specialist Agent

> 本文描述项目的**最终完整形态（设计目标）**，不是当前实现状态。
> 当前真实进度见 [PROJECT_PROGRESS.md](PROJECT_PROGRESS.md)；按最终简历组织的素材见 [PROJECT_RESUME_MATERIAL.md](PROJECT_RESUME_MATERIAL.md)。

---

## 0. 文档定位与设计原则

- 本 Spec 是"最终项目设计"基线：每个模块回答三个问题——**为什么存在、解决什么工程问题、与上下游如何协作**。
- 设计原则：真实、合理、可逐步实现。不为简历堆砌没有真实作用的技术；每个模块必须对应一个明确的工程问题。
- 当前代码是最终形态的一个**可运行子集**（任务生命周期、Planner/Executor 循环、工具注册表、沙箱集成、重试、API 均已落地），其余模块按本文设计逐步补齐。

---

## 1. 项目定位

**企业级 Agent 执行平台 / Document Specialist Agent**：一个把"用户自然语言任务"变成"沙箱内多步工具执行 + 产物落盘 + 全程可追踪"的 Agent 运行时平台。

- 面向对象：需要批量处理文档/数据的业务方（报表分析、数据清洗、文档转换）。
- 核心承诺：任务异步执行、状态可查询；执行隔离、失败可恢复、产物可下载、质量可评估。
- 与上游的关系：AIO Sandbox 提供隔离执行环境（Browser/Shell/File/Jupyter/MCP 聚合在单个容器），本项目补上它缺失的 **Agent 运行时层**：规划、执行、工具、任务、记忆、权限、评估、观测。

## 2. 真实业务场景

**主场景（文档处理）**：用户把一批 Excel/PDF 上传到 OSS，提交任务："分析 2026 Q2 各区域销售，找 Top3 产品，生成汇总报告（xlsx + md），给我下载链接。"

完整链路：

```text
API 提交 → Orchestrator 建任务（CREATED）
  → Planner 拆解计划（读文件 → 解析 → 分析 → 生成报告 → 落盘）
  → Executor 多轮 tool-calling（read_file / markitdown 解析 / run_python / save_report）
  → 沙箱内隔离执行
  → Validator 校验产物存在且非空
  → 结果上传 OSS，返回 presigned URL + 摘要
  → 任务 SUCCESS，全程可轮询
```

**其他场景**：
- 数据清洗/转换：CSV 清洗、格式转换、批量重命名。
- 长任务恢复：沙箱冷启动失败自动重试；任务中断后从最后成功步骤恢复。

**硬约束**：沙箱无状态、随时可销毁；所有持久数据在 OSS；敏感操作需要权限审批。

## 3. 总体架构

```text
                    ┌──────────────────────────────────────────────┐
                    │                User / HTTP API                │
                    │       提交任务 · 轮询状态 · 下载产物           │
                    └──────────────────────┬───────────────────────┘
                                           │
                    ┌──────────────────────▼───────────────────────┐
                    │            Agent Orchestrator                 │
                    │      任务编排、生命周期、失败回滚、恢复决策     │
                    └───────┬───────────────┬───────────────┬──────┘
                            │               │               │
                 ┌──────────▼───┐   ┌───────▼───────┐   ┌───▼─────────────┐
                 │   Planner    │   │   Executor    │   │ Task Management  │
                 │ LLM 结构化计划│   │ tool-calling  │   │ 状态机/存储抽象   │
                 │  + 记忆上下文 │   │ 循环 + 重试    │   │ (内存→Redis)     │
                 └──────────────┘   └───────┬───────┘   └─────────────────┘
                                           │ 工具调用
                 ┌──────────────────────────▼────────────────────────────┐
                 │                     Tool Registry                      │
                 │    Permission 检查 → 分发 → ToolResult(error_type)      │
                 └───┬───────────┬──────────────┬────────────┬───────────┘
                     │           │              │            │
               ┌─────▼───┐  ┌────▼─────┐  ┌─────▼──────┐  ┌──▼──────────┐
               │ Sandbox │  │ Doc Parse│  │ Memory     │  │ Report      │
               │ 隔离执行 │  │ (MCP)    │  │ (短/长)    │  │ (落盘)      │
               └─────────┘  └──────────┘  └────────────┘  └─────────────┘
                     │ 结果                                │
                     └───────────┬────────────────────────┘
               ┌─────────────────▼───────────────────────────────────────┐
               │      Validator / Recovery（校验产物 → 重试 / 重规划）     │
               └─────────────────┬───────────────────────────────────────┘
               ┌─────────────────▼───────────────────────────────────────┐
               │          Artifact Storage（MinIO / S3，presigned URL）   │
               └─────────────────┬───────────────────────────────────────┘
               ┌─────────────────▼───────────────────────────────────────┐
               │    Evaluation / Observability（指标、事件、任务维度日志）  │
               └──────────────────────────────────────────────────────────┘
```

**一次任务的完整时序**（与最终流程对齐）：

```text
User → Orchestrator → Planner → Executor → Tool Registry → Tool → Sandbox
     → Result → Validator / Recovery → Storage → Final Response
```

---

## 4. 模块设计

### 4.1 Agent Orchestrator

- **为什么存在**：业务方和 API 只需要面对一个入口，由它把"一次任务"串成完整生命周期。
- **解决的工程问题**：任务状态不可追踪、失败无痕、plan/执行/结果脱节；异步提交需要"先建任务拿 id、再按 id 执行"。
- **如何协作**：API → `orchestrator.run_task(task_id)`；内部依次调用 `TaskManager.start_task` → `Planner.plan` → `Executor.run` → Validator 校验 → `succeed_task`；异常时仅在非终态 `fail_task` 并记录错误，不覆盖已成功任务。
- **关键设计**：`run()` 与 `run_task()` 拆分；失败回滚；任务级 Recovery（重试/重规划）的决策点放在这里。

### 4.2 Planner

- **为什么存在**：复杂任务需要可执行、可校验的计划；让 LLM 在循环里自由发挥不可控。
- **解决的工程问题**：单次 LLM 调用无法完成多步任务；计划质量直接决定任务成功率。
- **如何协作**：输入 `user_input` + 短期记忆上下文 → 输出 `Plan(steps)`；Orchestrator 将计划交给 Executor；最终形态中 plan 与 `task.steps` 通过 `plan_id` 建立映射（当前 MVP 中 plan 仅作为执行上下文）。
- **关键设计**：用 `create_plan` 函数调用 + `tool_choice` 强制结构化输出；空计划/缺计划快速失败，而不是瞎执行。

### 4.3 Executor

- **为什么存在**：Agent 的核心循环——LLM 决策 → 工具执行 → 结果回传 → 再决策，直到产出最终答案。
- **解决的工程问题**：单轮脚本的工具结果从不回传；消息历史管理（`role:"tool"` + `tool_call_id`）；无限循环；上下文膨胀。
- **如何协作**：从 `ToolRegistry.to_openai_tools()` 拿 schema；`_invoke_tool` 内层集成重试；每个工具调用写一条 TaskStep；从短期记忆取上下文，大结果截断/摘要/引用 OSS key；`response.usage` 累加到任务指标。
- **关键设计**：`max_iterations` 终止；**失败不中断**——单步 FAILED 后把错误交还 LLM 决定换路；最终失败仍可被 Orchestrator 捕获标记任务 FAILED。

### 4.4 Tool Registry

- **为什么存在**：工具可插拔、schema 自描述、统一分发与观测。
- **解决的工程问题**：工具硬编码在入口脚本，新增工具要改动 Executor。
- **如何协作**：持有 `BaseTool` 集合；`to_openai_tools()` 批量生成 Function Calling 定义；`execute(name, args)` 前经过 Permission 层，返回 `ToolResult(success/output/error/error_type)`。
- **关键设计**：`BaseTool` ABC（`name` / `description` / `parameters_schema` / `execute`）；Executor 只依赖注册表接口，不认识具体工具。

### 4.5 Tool Calling

- **为什么存在**：LLM 与外部能力之间的消息协议闭环，是 Agent 的"手"。
- **解决的工程问题**：assistant 消息序列化、`tool_call_id` 匹配、参数 JSON 解析、结果回传格式；真实 SDK 与测试 fake 的结构差异。
- **如何协作**：Executor 实现闭环；Planner 用 `tool_choice` 强制 `create_plan`；工具用 OpenAI Function Calling schema 声明参数。
- **关键设计**：`_assistant_message` 把 pydantic 消息归一化成可回传 dict；`arguments` 可能为空要防御式解析；工具结果必须携带 `tool_call_id`。

### 4.6 Sandbox

- **为什么存在**：任意代码/命令在隔离容器中执行，是安全边界；同时提供统一环境（Shell/File/Jupyter/Browser/MCP）。
- **解决的工程问题**：二进制传输（xlsx 等）、输出解析不完整、鉴权缺失、路径安全、数据进出。
- **如何协作**：`SandboxClient` 防腐层供工具调用（`run_python` / `read_file`）；`HermesHookEngine` 的 pre/post hook 在 OSS↔沙箱之间搬运数据；任务结束后清理沙箱。
- **关键设计**：base64 二进制写入 + `download_file` 字节流读出；Jupyter 四类输出归一化；`X-AIO-API-Key` header 鉴权；`shlex.quote` 防注入删除；成功判据是 `status` 而非"有无输出"。

### 4.7 Retry / Recovery

- **为什么存在**：外部依赖（沙箱、OSS、LLM、网络）存在瞬态故障，一次抖动不该拖垮多步任务。
- **解决的工程问题**：无法区分"瞬态故障"与"确定性错误"；重试放哪一层；重试不可观测。
- **如何协作**：`ErrorType` 分类（工具返回 + 异常兜底 `classify_exception`）→ `RetryPolicy` 纯决策 → Executor 内层循环执行 sleep+重试；任务级 Recovery 由 Validator 触发（产物缺失/非法 → 重试该步骤或重规划）。
- **关键设计**：只重试瞬态（TRANSIENT/TIMEOUT）；指数退避 + 抖动防重试风暴；每次尝试记录 8 字段事件进 `task.metrics`；最终失败后错误交还 LLM，而不是直接判任务死刑。

### 4.8 Permission / Security

- **为什么存在**：沙箱隔离不是权限层；平台必须控制"能读什么文件、能执行什么命令、能调用什么工具"。
- **解决的工程问题**：`run_python` 任意代码、路径穿越、命令注入、密钥泄露、危险操作（删除/外发）。
- **如何协作**：工具调用进入 Registry 前，Policy Engine 决策（工具白名单 / 路径白名单 / 命令白名单 / 敏感操作审批）；工具声明自身权限需求；拒绝事件写入审计日志。
- **关键设计**：白名单优于黑名单；沙箱侧启用 API key；删除命令引用安全；可选人工审批流。

### 4.9 Short-term Memory

- **为什么存在**：LLM 上下文有限，长任务需要"该记住的上下文"而不是全部原始历史。
- **解决的工程问题**：消息越长越贵越慢；工具大结果会撑爆上下文；plan 与执行状态需要上下文衔接。
- **如何协作**：Executor 每次 LLM 调用前组装（系统提示 + 任务上下文 + 历史摘要 + 当前状态）；工具大结果截断/摘要或改为 OSS key 引用；Planner 读取任务约束。
- **关键设计**：上下文窗口管理；不把原始大文件塞进 prompt。

### 4.10 Long-term Memory

- **为什么存在**：同类任务不该重复犯错，领域知识（报告模板、常见坑）应可复用。
- **解决的工程问题**：历史任务结果与失败模式不可检索。
- **如何协作**：任务结束后写入（任务摘要、结果、失败原因）；Planner/Executor 检索相关历史注入 prompt。
- **关键设计**：先做关键词/标签检索，再演进向量检索；只存脱敏摘要，不存原始数据。

### 4.11 Task Management

- **为什么存在**：异步执行必须可追踪、可恢复、可审计。
- **解决的工程问题**：并发改坏状态、存储写死内存、跨进程不可用。
- **如何协作**：API/Orchestrator 调用 `TaskManager`；`TaskStore` ABC 从内存实现演进到 Redis；步骤轨迹同时是 Observability 与 Recovery 的数据源。
- **关键设计**：Task/TaskStep 两级状态机；`to_dict/from_dict` 序列化契约；双 `RLock`；Redis 版保留同样的状态语义。

### 4.12 Artifact Storage

- **为什么存在**：沙箱无状态，产物必须显式落盘，否则任务结束即丢失。
- **解决的工程问题**：二进制、大文件、下载鉴权、产物生命周期。
- **如何协作**：`ReportTool/save_report` 读沙箱字节 → 上传 OSS → 返回 presigned URL；pre-hook 从 OSS 进沙箱；产物元数据写入任务结果。
- **关键设计**：bucket 懒创建（构造不触网）；presigned URL 有时效；可演进对象过期策略。

### 4.13 Evaluation

- **为什么存在**：没有指标就无法迭代；交付和面试都需要可核验的质量证据。
- **解决的工程问题**：当前 `metrics` 预留但未落库；不知道成功率、耗时、token 分布。
- **如何协作**：Executor/重试/记忆层写入 `metrics` 与事件；固定 Eval 集（任务 + 期望产物）批量跑；输出报告（成功率、耗时、token、重试率、失败原因 Top）。
- **关键设计**：指标口径明确；离线 Eval 集可复跑；与 Observability 共享事件源。**红线：只统计真实运行数据，不编造。**

### 4.14 Observability

- **为什么存在**：排查"任务到哪一步挂了"是运维刚需。
- **解决的工程问题**：没有任务维度日志、事件不落库。
- **如何协作**：步骤轨迹 + `retry_events`（8 字段）+ usage 统计 + 带 `task_id` 的日志；可选接入 OpenTelemetry。
- **关键设计**：事件扁平可查询；`task_id` 贯穿所有日志与事件。

### 4.15 Multi-Agent / Workflow（后续可选）

- **为什么存在**：单 Agent 有上下文与工具边界；复杂流程需要分工、并行与审批。
- **解决的工程问题**：子任务并行、条件分支、人工审批、跨 Agent 结果传递。
- **如何协作**：Orchestrator 之上加 Workflow 引擎（DAG：串行/并行/条件/审批）；子任务作为独立 task 执行后汇总。
- **状态**：DEFERRED（后续可选），不在简历核心承诺内。

---

## 5. 部署形态

- 当前：`docker-compose` 一键起 sandbox + MinIO + API（本地开发/演示）。
- 演进：Redis（TaskStore + 队列）→ 无状态 API 多实例 → worker 进程消费任务；任务存储、产物存储、队列全部外部化，支撑横向扩展。

## 6. 从当前实现到最终形态的路线

详见 [PROJECT_PROGRESS.md](PROJECT_PROGRESS.md)。原则：**任何模块标 DONE 必须有代码 + 测试 + 验证证据**；PLANNED 模块进入简历前必须真正实现。
