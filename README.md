# Document Specialist Agent

面向**文档分析与代码执行**场景的本地 Agent Harness。

大模型的工具调用被放进一个可追踪、有执行限制、能处理异常的程序里：模型负责规划步骤、调用工具、根据执行结果继续处理；运行时负责隔离执行、任务生命周期、产物交付与执行轨迹。

基于 [agent-infra/sandbox](https://github.com/agent-infra/sandbox)（AIO Sandbox，v1.11.0 / Python SDK 0.0.30）二次开发。上游提供隔离的执行环境（Shell / File / Jupyter / Browser / MCP），本项目补上它之外的 **Agent 运行时层**：规划、执行、工具、任务、权限、产物、恢复。

---

## 1. 要解决的问题

直接与大模型对话，通常能拿到处理建议或代码；但从"代码"到"真正执行"，再到"拿到可用文件"，中间仍然依赖人工：复制代码、准备环境、上传文件、取回结果、处理报错。

本项目把这一段做成运行时：

- **可追踪**：任务与步骤两级状态、工具调用、重试、校验、恢复事件全部留痕；
- **有执行限制**：代码只在容器沙箱内执行，受超时、资源、工具白名单与路径白名单约束；
- **能处理异常**：瞬态故障按指数退避重试，产物不符则重新生成，进程重启后回收僵死任务；
- **能跨任务复用经验**：长期记忆按用户和项目保存在本地 SQLite，经过来源校验后才写入；
- **能独立评测效果**：版本化案例通过同一 Orchestrator 入口运行，从任务轨迹计算完成率、工具选择、步骤完成、恢复、Token 与时延；
- **交付产物**：结果落盘到对象存储，返回有时效的下载链接。

典型任务："沙箱里有一份 sales.xlsx，按区域汇总收入，把结果存成 reports/q3_summary.xlsx 并给我下载链接。"

### 当前能力总览

当前 `main` 已合入 P0-1～P1-3，Agent Harness 具备以下能力：

| 能力 | 当前实现 |
| --- | --- |
| 结构化规划 | Planner 生成带步骤 ID、依赖关系和完成条件的 DAG 计划，Runtime 校验并持久化版本 |
| 受计划约束的执行 | Tool Call 绑定计划步骤，只调度依赖已满足的步骤，并用证据判定完成 |
| 局部重规划 | 保留已完成步骤，只替换未完成部分；限制重规划次数并记录完整事件 |
| 多轮 Tool Calling | Executor 驱动“模型决策 → 工具执行 → 结果回注 → 再决策”的 Agent Loop |
| 工具治理 | 可插拔 Registry、JSON Schema 校验、权限白名单、任务级参数重写和统一结果封装 |
| 大结果治理 | 统一卸载大型工具结果，以随机 `result_ref` 在当前任务内分页回读 |
| 上下文治理 | Token 估算、完整消息组压缩、关键信息保护、失败重试、熔断和确定性裁剪 |
| 安全执行 | Docker Sandbox、资源限制、任务目录隔离、路径/对象前缀校验和 API Token |
| 产物交付 | 输入装载、对象存储、产物存在性与大小校验、预签名下载链接及有限修复 |
| 状态与恢复 | Task/TaskStep 状态机、JSON 原子落盘、有界 worker、僵死任务回收和错误分类重试 |
| 本地长期记忆 | SQLite 持久化、用户/项目作用域、策略过滤、去重、关键词 Top-K 和按需召回 |
| 独立 Evaluation | 版本化数据集、Fake/Real 运行模式、轨迹评分及 JSON/Markdown 报告 |
| 资源计量与预算 | 按任务/阶段/轮次统计 Token 和时延，估算费用，并用 soft/hard budget 控制任务 |

> 这里的“具备”表示代码与配套学习/验证资产已经进入 `main`，不等同于所有能力都已在
> 当前环境完成独立回归或真实 Docker/LLM 验证。实际验证状态以
> [`docs/verification/`](docs/verification/) 下各编号记录为准。

---

## 2. 架构

```text
                     HTTP API（FastAPI）
          POST /tasks · GET /tasks/{id} · /health
                           │
                  TaskWorkerPool（有界 worker）
                           │
                  AgentOrchestrator
        ┌──────────────┬──────────────┬──────────────┐
        │              │              │              │
   TaskManager      Planner        Executor     InputStager
   状态机/落盘    结构化计划    多轮 tool-loop   OSS → 沙箱
        │              │              │              │
        └──────────────┴──────┬───────┴──────────────┘
                              │
                        ToolRegistry
              权限校验 · Schema 校验 · 任务级参数重写
        ┌──────────┬──────────┬──────────────┬──────────┐
        │          │          │              │          │
   run_python  read_file  parse_document  save_report
        │          │          │              │          │
        └──────────┴──────────┴──────────────┴──────────┘
                              │
                AfterToolCall → ToolOutputStore
                    大结果卸载 · 分页二次回读
                              │
                   Docker Sandbox（隔离执行）
                              │
              ArtifactValidator → 对象存储（MinIO/S3）
                              │
                        预签名下载链接
```

**一次任务的时序**：提交 → 建任务 → 装载输入 → 规划 → 多轮工具调用（沙箱执行）→ 产物校验 → 成功/失败 → 下载链接。

长期记忆位于这条主链的两端：任务开始时按 `user_id/project_id` 召回并注入 Planner、
Executor；任务成功后才执行候选提取、规则过滤、去重和 SQLite 写入。执行中还可通过
`search_memory` 做一次受作用域约束的二次召回。

Evaluation 位于主链之外：它提交版本化案例，复用同一条 Orchestrator 调用链，再只读
Task、TaskStep、plan_events 和 metrics 计算效果指标，不参与 Agent 的执行决策。

Metering 横切所有模型调用：`plan / replan / context_compaction / execute /
memory_capture` 的原始事件先写入 `llm_events`，再聚合为 task、phase、iteration 三层
`metrics.usage`。核心 Agent Loop 的累计 Token 达到 soft budget 后要求 ContextManager
主动收敛，达到 hard budget 后终止任务。

---

## 3. 核心能力

### Agent Loop

- **Planner**：用 `create_plan` 函数调用生成结构化计划，每步包含 `step_id`、`depends_on`、`completion_criteria` 和可选工具。运行时校验 Schema、ID 唯一性、依赖存在性与无环性；非法计划直接失败。
- **计划留存**：版本 1 的初始计划在执行前写入 `Task.plan`，查询 API 可返回完整计划；旧任务没有计划时返回 `null`。计划步骤与 `Task.steps` 中的真实工具调用记录分开保存。
- **执行绑定**：每个 Tool Call 记录 `plan_step_id` 与 `tool_call_id`，只有依赖已完成的计划步骤可以调度，已完成步骤不可重放；步骤完成由 `complete_plan_step` 携带的 `completion_criteria` 证据判定，工具返回 success 不等于业务步骤完成。
- **局部重规划**：工具不可恢复失败、数据缺失、完成条件未满足或依赖失效时，模型可请求 `request_replan`；新计划版本 +1，只替换未完成部分，预算由 `max_replans` 限制，超限任务明确失败。`Task.plan_events` 记录创建、绑定、完成、失败与重规划全过程。
- **Executor**：多轮 tool-calling 循环——模型决策 → 工具执行 → 结果回传 → 再决策，直到产出最终答案。
- **大结果卸载**：所有真实工具结果在进入任务记录和模型上下文前统一经过 `AfterToolCall` Hook；小结果原样回注，大结果完整写入本地 `ToolOutputStore`，上下文只保留预览、大小、内容类型、随机 `result_ref` 和回读提示。
- **受控二次回读**：模型只能调用 `read_tool_output(result_ref, offset, limit)` 分页读取；`task_id` 由 Registry 注入而不暴露给模型，引用按任务隔离，单次回读受字符上限约束。
- **Token 预算**：每次执行模型调用前同时估算消息与工具 Schema；估算优先使用真实 usage 校准，其次使用可选 tokenizer，最后按中英文字符密度折算。soft / hard / target 三档阈值为回答预留固定输出空间。
- **完整消息组压缩**：只将较早的 `assistant tool_calls + 对应 tool results` 完整组交给独立压缩调用，绝不拆散调用与结果；结构化摘要保留任务目标、当前计划、确认事实、未完成事项和全部 `result_ref`。
- **压缩熔断**：压缩请求超长或响应非法时，按完整组移除最早历史后有限重试；连续失败打开熔断器，后续改用确定性整组裁剪，仍超过 hard limit 才明确终止任务。
- **终止控制**：`max_iterations` 限制单次执行的轮数；单步失败不中断，错误交回模型决定换路。
- **可观测**：每次 LLM 调用、工具调用、上下文决策和记忆读写都写入任务指标（`llm_events` / `retry_events` / `context_events` / `memory_events`）。

### Token、时延与成本计量

- **统一 usage**：兼容 OpenAI 风格 `prompt_tokens_details.cached_tokens` 与 DeepSeek 风格
  `prompt_cache_hit_tokens`，统一记录 prompt / completion / total / cache tokens、模型名、
  单次时延和重试状态。
- **三层聚合**：原始事件保持追加写入；`UsageMeter` 将其派生为 task 总量、phase 总量和
  `phase:iteration` 总量。失败重试计入 attempts 与时延，但没有 provider usage 时不虚构
  Token。
- **版本化估价**：价格通过环境变量显式配置并带 `pricing_version`；缓存输入、普通输入和
  输出分别计价。未配置价格或遇到未知模型时费用为 `null`，不会把未知成本算成 0。
- **累计预算**：`plan / replan / context_compaction / execute` 消耗共同预算；达到 soft
  budget 后下一轮强制走上下文收敛路径，达到 hard budget 后抛出明确异常并使任务失败。
  成功后的 best-effort 记忆提取仍计量，但不消耗核心执行预算。
- **费用语义**：`estimated_cost_usd` 是按本地版本化价格表计算的估算值，不是供应商最终
  账单；供应商舍入、折扣、峰谷价格或隐藏 Token 都可能造成差异。

### 本地长期记忆

- **四类记忆**：用户明确偏好、项目固定约束、已确认业务事实、成功验证的处理经验；
  每条记录包含 `source_task_id`、来源类型、证据摘录、置信度、状态和时间。
- **保守写入**：模型只负责提出候选；Runtime 再检查来源原文、类型与来源是否匹配、
  置信度、长度和敏感信息。推测、凭证和无证据内容不会落库。
- **可解释召回**：SQLite 先按 `user_id/project_id/status/type` 过滤，再在本地按关键词
  重合、短语命中、记忆类型和置信度计算 Top-K；当前不使用 Embedding 或向量数据库。
- **作用域隔离**：模型不能提供 user/project 参数；`search_memory` 根据当前 task_id
  从 TaskManager 获取作用域，避免跨任务串线。
- **生命周期**：重复内容不重复写入；记忆可失效或软删除，非 ACTIVE 记录不再注入。
  当前用户输入与历史记忆冲突时，以当前输入为准。
- **失败隔离**：召回或写入失败只产生 `memory_events`，不会把已成功的文档任务改成失败。

### 独立 Evaluation

- **版本化案例**：`evaluation/datasets/harness_v1.json` 固定九类任务，覆盖文档解析、
  结构化统计、产物生成、缺失数据、参数错误、瞬态故障、大结果、局部重规划和记忆召回。
- **统一入口**：Fake LLM 只替代模型与外部工具，案例仍经过 Orchestrator、Executor、
  Tool Registry、重试、Hook 和 TaskManager，不复制一套“评测专用 Agent”。
- **确定性评分**：Scorer 只读取持久化轨迹，不让模型给自己的答案打分；工具序列、计划
  完成证据、产物校验、失败恢复和事件要求都能回查。
- **九项聚合指标**：任务完成率、工具选择正确率、计划步骤完成率、产物校验通过率、
  平均 Tool Call、平均重规划、失败恢复率、平均 Token 和 P95 时延。
- **Fake/Real 分离**：Fake 模式用于稳定回归；真实模型模式必须显式传入
  `--allow-external`，报告固定记录模型名、Prompt 版本和数据集版本。

### 任务生命周期

- Task / TaskStep 两级状态机（`CREATED → RUNNING → SUCCESS/FAILED`），非法流转直接拒绝。
- 异步执行：`POST /tasks` 立即返回任务 id，由有界 worker 池执行；超出 `TASK_MAX_WORKERS + TASK_MAX_PENDING` 返回 429。
- 落盘留存：每个任务写成一份 JSON（原子写），重启后 `GET /tasks/{id}` 仍能拿到完整轨迹。
- 僵死回收：进程被杀会留下 RUNNING 任务，启动时与运行期按"无进展超时"判 FAILED 并记录 `recovery_events`（只收敛状态，不声称终止已在执行的代码）。

### 工具管理

- **可插拔 Tool Registry**：注册即生效，Schema 自动生成给模型，新增工具不需要改 Executor。
- 注册时校验 JSON Schema（含禁止外部 `$ref`），调用前做参数 Schema 校验与权限校验。
- 内置工具：

  | 工具 | 作用 |
  | --- | --- |
  | `run_python` | 在沙箱内执行 Python（默认超时；超时或状态未知时终止任务，不重放） |
  | `read_file` | 读取文本文件；遇到二进制文档直接提示改用 `parse_document` |
  | `parse_document` | PDF / Excel / PPTX / DOCX / CSV / JSON → Markdown |
  | `save_report` | 沙箱文件 → 对象存储 → 预签名下载链接 |
  | `read_tool_output` | 按逻辑引用分页回读被卸载的大型工具结果 |
  | `search_memory` | 在当前用户/项目作用域内按关键词二次召回长期记忆 |

### 隔离与安全

- **容器沙箱**：Agent 生成的代码只在 AIO Sandbox 容器内执行；compose 中限制了 CPU / 内存 / PID 配额。
- **任务级隔离**：每个任务拥有独立沙箱目录 `tasks/<task_id>` 与独立对象前缀 `reports/<task_id>/`。工具参数在调用前被重写进任务命名空间，跨任务路径直接拒绝；`run_python` 的工作目录也由运行时注入，模型无法写到共享根目录。两个同名输入、同名产物的任务并发执行不会互相覆盖。
- **结果引用隔离**：大结果引用采用 128 位随机逻辑 ID，不接受文件路径；Registry 只把当前任务 ID 注入回读工具，非法引用、路径式引用和跨任务引用统一拒绝。
- **权限白名单**：工具白名单 + 权限声明 + 工作区路径校验（拦截 `..`、绝对路径、符号链接逃逸）+ 对象前缀校验。
- **输入装载**：`input_files` 指定的对象键必须落在 `INPUT_PREFIX` 下，越界、缺失、空对象都会让任务以明确原因失败。
- **API 鉴权**：设置 `API_TOKEN` 后，所有请求需带 `X-API-Token`（常量时间比较）。

### 产物交付与校验

- `save_report` 上传后返回结构化产物记录（对象键、字节数、类型）与预签名 URL。
- **产物校验**：任务成功前逐个回查对象存储（存在、非空、字节数一致），结果写 `validation_events`；校验不过不会出现"模型说做完了但产物不对"的假成功。
- `require_artifact: true` 时，"一个产物都没产出"同样算校验失败。
- 预签名 URL 可用 `MINIO_PUBLIC_ENDPOINT` 按对外地址签名（SigV4 覆盖 Host，必须用它签名而非事后替换主机名）。

### 异常恢复

- **错误分类**：`TRANSIENT / TIMEOUT / INVALID_ARGUMENT / PERMISSION_DENIED / EXECUTION / BUSINESS`，只重试可恢复的瞬态故障。
- **指数退避 + 抖动**：避免重试风暴；每次尝试记录 8 字段事件，便于定位失败链路。
- **LLM 调用边界**：单次调用超时（默认 60s），关闭 SDK 自带隐藏重试，由统一策略重试，避免任务长时间卡在 RUNNING。
- **产物修复重试**：校验失败时在 `TASK_MAX_RECOVERY_ATTEMPTS` 内带着失败原因重跑，提示模型重新生成产物；仍不通过才判 FAILED。

---

## 4. 目录结构

```text
document-specialist-agent/
├── agent/            # Orchestrator / Planner / Executor / LLM 客户端 / 产物校验 / 装配
├── api/              # FastAPI 入口 + 有界 worker 池
├── core/             # 配置（pydantic-settings）
├── task/             # 任务模型、状态机、Manager、落盘存储
├── tools/            # BaseTool + Registry + 具体工具
├── sandbox/          # 沙箱客户端防腐层、输入装载、Hook
├── security/         # 权限与路径/对象键策略
├── storage/          # 对象存储封装（MinIO / S3 兼容）
├── retry/            # 错误分类 + 重试策略
├── context/          # 大结果卸载、Token 估算、消息分组、摘要压缩与熔断
├── memory/           # SQLite Store、候选提取、写入策略、关键词召回与注入
├── evaluation/       # 版本化案例、Runner、轨迹评分、Fake/Real 适配与报告
├── metering/         # Token/时延聚合、版本化价格表与任务级预算控制
├── demo/             # 端到端与离线演示脚本
├── docs/design/      # 各模块设计笔记
└── tests/            # 离线单元/集成测试（fake LLM / SDK / S3）
```

---

## 5. 快速开始

前置：Python 3.10+（本地实测 3.13.2）、Docker Desktop、一个 OpenAI 兼容的模型 API Key（默认 DeepSeek）。

```powershell
# 1. 依赖
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 启动沙箱与对象存储
docker compose up -d

# 3. 配置：把 .env.example 复制为 .env，至少填 LLM_API_KEY
Copy-Item .env.example .env

# 4. 跑测试（全部离线，不需要 Docker 与 API Key）
.venv\Scripts\python.exe -m pytest -q

# 5. 启动 API
.venv\Scripts\python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8000
```

可选演示脚本（`retry_demo`、`security_demo` 可离线运行；`run_demo` 与
`security_smoke` 需要对应的 Docker/外部依赖）：

```powershell
.venv\Scripts\python.exe -m demo.run_demo        # CSV → OSS → 沙箱 → Agent → 产物链接
.venv\Scripts\python.exe -m demo.retry_demo      # 重试策略 4 个场景
.venv\Scripts\python.exe -m demo.security_demo   # 权限与拒绝审计 6 个场景
.venv\Scripts\python.exe -m demo.security_smoke  # 沙箱配额与会话检查
```

独立 Agent Evaluation：

```powershell
# 确定性 Fake LLM 评测：不调用真实模型、Docker 或 MinIO
.venv\Scripts\python.exe -m evaluation --mode fake

# 真实模型评测：会使用当前 .env 中的模型、Sandbox、MinIO 和数据集输入
.venv\Scripts\python.exe -m evaluation --mode real --allow-external `
  --prompt-version harness-prompts-v1
```

两种模式都会在 `.data/evaluation/reports/` 生成同一结构的 JSON 与 Markdown 报告。

---

## 6. API

| 端点 | 说明 |
| --- | --- |
| `POST /tasks` | 提交任务，立即返回任务（`CREATED`）；队列满返回 **429** |
| `GET /tasks/{id}` | 查询状态、步骤、产物、指标事件；不存在返回 404 |
| `GET /tasks` | 列出全部任务 |
| `GET /memories` | 按 user/project/status 查看本地记忆 |
| `POST /memories/{id}/invalidate` | 将当前作用域的一条记忆标记为失效 |
| `DELETE /memories/{id}` | 软删除当前作用域的一条记忆 |
| `GET /health` | 服务状态与 worker 池统计 |

请求体（`POST /tasks`）：

```json
{
  "user_input": "读取 sales.xlsx，按区域汇总收入，保存为 reports/q3_summary.xlsx",
  "user_id": "local-user",
  "project_id": "sales-analysis",
  "input_files": [{"oss_key": "raw/sales.xlsx", "filename": "sales.xlsx"}],
  "require_artifact": true
}
```

- `input_files`：先把对象从存储装载进沙箱，并把沙箱内的绝对路径告诉模型（可选）。
- `require_artifact`：要求任务必须产出可下载产物（可选，默认 false）。
- `user_id/project_id`：本地记忆的逻辑作用域，不是登录身份或多租户鉴权声明。

调用示例（设置了 `API_TOKEN` 时需带请求头）：

```powershell
curl -X POST http://127.0.0.1:8000/tasks -H "Content-Type: application/json" ^
  -H "X-API-Token: <你的 API_TOKEN>" ^
  -d "{\"user_input\":\"读取 input.csv，统计各部门平均薪资并保存为 reports/summary.csv\",\"require_artifact\":true}"

curl http://127.0.0.1:8000/tasks/<task_id> -H "X-API-Token: <你的 API_TOKEN>"
```

> 未设置 `API_TOKEN` 时不做鉴权：任何能访问该端口的进程都能提交任务，而任务会在沙箱内执行模型生成的代码。仅限本机自用；一旦监听 `0.0.0.0` 或对外暴露，必须设置 `API_TOKEN`。

---

## 7. 配置项

全部通过 `.env` 或环境变量注入，完整示例见 [.env.example](.env.example)。

| 分组 | 变量 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 沙箱 | `SANDBOX_BASE_URL` | `http://localhost:8080` | AIO Sandbox 地址 |
| 沙箱 | `SANDBOX_WORKSPACE` | `/home/gem/workspace` | 沙箱工作区根目录 |
| 沙箱 | `SANDBOX_API_KEY` | 空 | 沙箱鉴权头；为空则沙箱 API 完全开放 |
| 沙箱 | `SANDBOX_DEFAULT_TIMEOUT` / `SANDBOX_MAX_TIMEOUT` | `30` / `120` | 代码执行默认与最大超时（秒） |
| 沙箱 | `SANDBOX_CPUS` / `SANDBOX_MEMORY` / `SANDBOX_PIDS_LIMIT` | `2.0` / `4g` / `512` | 容器配额（compose 使用） |
| 权限 | `ALLOWED_TOOLS` | 六个内置工具 | 工具白名单（JSON 数组） |
| 权限 | `ALLOWED_PERMISSIONS` | 含 `tool_output.read` / `memory.read` | 权限白名单 |
| 权限 | `REPORT_PREFIX` / `INPUT_PREFIX` | `reports` / `raw` | 产物对象前缀 / 输入对象前缀 |
| 存储 | `MINIO_ENDPOINT` | `http://localhost:9000` | 对象存储地址 |
| 存储 | `MINIO_PUBLIC_ENDPOINT` | 空 | 生成下载链接用的对外地址 |
| 存储 | `MINIO_BIND_HOST` | `127.0.0.1` | MinIO 在宿主机上的监听地址（`0.0.0.0` = 局域网可访问） |
| 模型 | `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | — / `https://api.deepseek.com` / `deepseek-chat` | OpenAI 兼容模型配置 |
| 模型 | `LLM_TIMEOUT` / `LLM_MAX_ATTEMPTS` | `60` / `3` | 单次调用超时与重试预算 |
| 模型 | `LLM_RETRY_BASE_DELAY` / `LLM_RETRY_MAX_DELAY` | `1` / `10` | 退避区间（秒） |
| 计量 | `LLM_PRICING_VERSION` + 三档 `*_PRICE_USD_PER_MILLION` | 空 | 可选版本化单价；四项必须同时配置 |
| 上下文 | `CONTEXT_TARGET_TOKENS` / `CONTEXT_SOFT_LIMIT_TOKENS` / `CONTEXT_HARD_LIMIT_TOKENS` | `32000` / `48000` / `56000` | 压缩目标、触发阈值与输入硬上限 |
| 上下文 | `CONTEXT_WINDOW_TOKENS` / `CONTEXT_OUTPUT_RESERVE_TOKENS` | `64000` / `8000` | 模型窗口与回答预留空间 |
| 上下文 | `CONTEXT_RECENT_GROUPS` | `2` | 优先保留的最近完整消息组数量 |
| 记忆 | `MEMORY_ENABLED` / `MEMORY_DB_PATH` | `true` / `.data/memory/memory.db` | 是否启用本地长期记忆及 SQLite 文件位置 |
| 记忆 | `MEMORY_RECALL_TOP_K` / `MEMORY_MIN_CONFIDENCE` | `5` / `0.8` | 初始召回数量与候选写入置信度门槛 |
| 记忆 | `MEMORY_MAX_CONTENT_CHARS` | `800` | 单条长期记忆的最大字符数 |
| 任务 | `TASK_STORE_DIR` | `.data/tasks` | 任务落盘目录 |
| 任务 | `TASK_STALE_AFTER_SECONDS` / `TASK_REAPER_INTERVAL_SECONDS` | `1800` / `60` | 僵死判定阈值与巡检间隔 |
| 任务 | `TASK_MAX_WORKERS` / `TASK_MAX_PENDING` | `2` / `32` | 并发 worker 数与可排队数 |
| 任务 | `TASK_MAX_RECOVERY_ATTEMPTS` | `1` | 产物校验失败后的修复重试次数 |
| 任务 | `TASK_SOFT_TOKEN_BUDGET` / `TASK_HARD_TOKEN_BUDGET` | `200000` / `300000` | 累计核心模型调用的收敛/终止阈值 |
| 文档 | `DOCUMENT_MAX_CHARS` | `20000` | `parse_document` 返回给模型的字符预算 |
| 接口 | `API_TOKEN` | 空 | 为空则不做鉴权 |

---

## 8. 测试

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_harness_v1_scenarios.py
.venv\Scripts\python.exe -m pytest -q tests/test_memory_store.py tests/test_memory_policy.py tests/test_memory_service.py tests/test_memory_prompting.py tests/test_memory_extractor.py tests/test_orchestrator_memory.py tests/test_memory_api.py
.venv\Scripts\python.exe -m pytest -q tests/test_evaluation.py
.venv\Scripts\python.exe -m pytest -q tests/test_metering.py tests/test_llm_client.py tests/test_config.py
.venv\Scripts\python.exe -m pytest -q
```

`tests/test_harness_v1_scenarios.py` 是 Harness V1 的八条可执行学习场景，覆盖静态计划、
工具失败换路、局部重规划、大结果卸载与回读、上下文压缩与熔断，以及两类循环预算。
默认测试全部离线：LLM、沙箱 SDK、S3 均使用 fake，不需要 Docker 与 API Key。

README 不固化易过期的 passed 数量；当前版本的环境、commit、完整输出和真实 Docker
结果统一记录在
[`docs/verification/06_harness_v1.md`](docs/verification/06_harness_v1.md)。Windows
未开启开发者模式时，符号链接路径守卫用例可能因无法创建符号链接而跳过。

真机链路（Docker + LLM Key）另行验证，覆盖：输入装载、沙箱执行、PDF/Excel 解析、产物上传与预签名下载、并发任务隔离、产物校验与修复重试、僵死任务回收。

---

## 9. 已知限制

- **沙箱是单容器共享内核**：文件层面已按任务隔离，但代码执行仍共用同一个容器；要做强隔离或真正并行，需要一任务一容器或沙箱池。
- **任务存储是单进程文件存储**：多 worker 部署需要换成 Redis 等共享存储。
- **无多租户**：没有账号体系，`GET /tasks` 返回该实例的全部任务。
- **记忆作用域不是鉴权边界**：`user_id/project_id` 用于本地数据分组，API 调用者仍由
  `API_TOKEN` 统一保护；当前关键词召回可解释但不理解同义词，语义召回属于后续增强。
- **成本只是本地估算**：未配置版本化单价时 `estimated_cost_usd` 为 `null`；即使配置，
  也不能替代供应商账单。hard budget 使用 provider 已返回的 usage，因此能拒绝越界响应并
  阻止后续调用，但不能预先阻止“首次把累计量推过阈值”的那一次请求。
- **Evaluation 当前不做语义答案裁判**：P1-2 只根据确定性执行轨迹和产物校验评分，
  不使用 LLM-as-a-Judge；Fake 高分证明 Harness 控制流可重复，不等于真实模型质量高。
- **无 CI 与部署产物**：仓库没有 GitHub Actions，也没有 API 服务自身的 Dockerfile 与反向代理配置。
- 沙箱容器使用 `seccomp:unconfined`，且默认不启用 `SANDBOX_API_KEY`——这是本地原型的安全边界，不适合直接暴露到公网。

---

## 10. 设计文档

- `docs/design/01_task_module.md` ~ `10_security_module.md`：各模块设计笔记（统一 10 小节模板）。
- `docs/verification/01_security.md`：安全模块的验证记录（含未验证边界）。
- `docs/verification/06_harness_v1.md`：Harness V1 证据矩阵与当前回归/真实环境验证记录。
- `docs/design/11_memory_module.md`：本地长期记忆的写入、召回、隔离与失败边界。
- `docs/verification/07_local_memory.md`：P1-1 验收矩阵与待执行命令。
- `docs/design/12_evaluation_module.md`：独立 Evaluation 的案例、评分、运行与报告链路。
- `docs/verification/08_evaluation.md`：P1-2 验收矩阵与待执行命令。
- `docs/design/13_metering_and_budget.md`：P1-3 计量、估价与累计预算的设计和学习说明。
- `docs/verification/09_metering_budget.md`：P1-3 验收矩阵与待执行命令。
