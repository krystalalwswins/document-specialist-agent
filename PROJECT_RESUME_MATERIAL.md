# PROJECT RESUME MATERIAL —— Document Specialist Agent

> 面向面试的完整项目素材，按 12 个小节组织。所有表述基于仓库实际代码、提交记录与设计文档，可直接用于简历项目描述和面试问答准备。

---

## 1. 项目背景

本项目基于开源项目 [agent-infra/sandbox](https://github.com/agent-infra/sandbox)（AIO Sandbox，镜像 v1.11.0 / Python SDK 0.0.30）二次开发。

上游项目解决的是“Agent 的基础设施”：把 Browser / Shell / File / Jupyter / MCP 聚合进一个 Docker 容器，提供统一沙箱。但它**不是业务 Agent**——没有任务生命周期、没有工具注册表、没有“计划-执行”循环。接手时仓库里的 `agent_core.py` 只是一个单轮脚本：LLM 调用一次 → 执行一次工具 → 结果不回传 → 跑完即丢；文件传输按 UTF-8 解码（xlsx 必炸）；模块 import 即触网；任务失败无痕。

我的工作是把这段“能跑通的 demo 脚本”重构成一个**有完整任务生命周期、可插拔工具注册表、多轮计划-执行循环的 Agent 运行时**，并把沙箱（隔离执行层）和对象存储（结果层）接成一条可演示的垂直链路。

## 2. 项目目标

1. **多轮 Agent 运行时**：Planner 用 LLM 结构化分解任务 → Executor 多轮 tool-calling 循环，工具结果真正回传 LLM，直到产出最终答案。
2. **任务全生命周期可追踪**：任务/步骤两级状态机（CREATED→RUNNING→SUCCESS/FAILED），任何时刻可回答“任务到哪一步了”，失败有迹可循。
3. **工具可插拔**：`BaseTool` 抽象 + `ToolRegistry` 动态注册，工具自描述 JSON Schema，新增工具不改 Executor。
4. **沙箱隔离执行 + 对象存储结果层**：OSS→沙箱（pre-hook）→执行→沙箱→OSS（post-hook）二进制安全闭环，成果以 presigned URL 交付。
5. **工程化**：全离线可测（fake LLM/SDK/S3）、依赖注入、对外 HTTP API、工具失败自动重试、每个模块配套设计文档。

**MVP 明确不做**（记入文档，避免范围蔓延）：权限/安全层、持久化任务队列、Memory、MCP 文档解析接入、评估体系。

## 3. 架构

```text
                 User
                  |
                  |
             API Layer  (FastAPI: POST /tasks, GET /tasks/{id})
                  |
                  |
          Agent Orchestrator  (生命周期编排)
                  |
        --------------------
        |        |         |
     Planner   Memory    Tool Registry
        |                  |  (BaseTool: run_python / read_file / save_report)
        |
    Task Executor  (多轮 tool-calling + RetryPolicy)
        |
        |
   Sandbox Runtime  (AIO Sandbox Docker 容器, 防腐层 SandboxClient)
        |
        |
 Result Storage  (MinIO/S3, StorageManager + presigned URL)
```

**核心分层原则：**

- **防腐层**：`SandboxClient` 把 SDK 响应归一化成自己的 `ExecutionResult`，上层（工具/Executor）不直接依赖 SDK 细节。
- **接口化**：`TaskStore` ABC（存储可换）、`BaseTool`（工具可插）、`LLMClient`（LLM 可注入）。
- **组合根**：`agent/wiring.py` 的 `build_orchestrator()` 一处装配全部依赖，demo 和 API 共用。
- **数据边界**：“执行在沙箱、成果在 OSS”——沙箱无状态随时可弃，产物必须经 `save_report` 显式落盘。

## 4. 核心模块

| 模块 | 职责 | 关键设计 |
| --- | --- | --- |
| `task/` | 任务领域模型 + 生命周期管理 | 两级状态机（Task/TaskStep）、`to_dict/from_dict` 序列化契约、`TaskStore` ABC、双 `RLock` 并发安全 |
| `agent/` | Planner / Executor / Orchestrator | `create_plan` 函数调用强制结构化计划；多轮 tool-calling 循环（`max_iterations=8`）；失败不中断（单步 FAILED 后交还 LLM 决策） |
| `tools/` | 工具抽象与三个具体工具 | schema 自描述；`save_report` 为成果落盘唯一出口 |
| `sandbox/` | SDK 防腐层 + 数据搬运 Hook | base64 二进制安全传输；Jupyter 四类输出归一化；header 鉴权；`shlex.quote` 防注入删除 |
| `storage/` | MinIO/S3 对象存储封装 | 懒创建 bucket（构造不触网）、404 白名单、`StorageError` 错误包装、presigned URL |
| `retry/` | 工具层重试 | `ErrorType` 6 类错误分类 + `RetryPolicy` 纯决策（指数退避+抖动）+ Executor 内层循环集成 |
| `api/` | 最小 HTTP 层 | 后台线程异步执行 + 轮询；`create_app` 工厂可注入测试 |
| `core/` | 类型化配置 | pydantic-settings + `lru_cache` 单例，import 无副作用 |

## 5. 自己完成的工作

独立完成从 0 到 Phase 2 的全部工作（11 个提交，2026-08-30 ~ 08-31）：

1. **上游调研与核验**：核实 Python SDK 要求 3.8+（本机 3.7 不满足，迁移到 3.13.2）；PyPI 最新版实为 0.0.30（原 `>=0.1.0` 不存在）；直接读 SDK 源码确认二进制传输走 `encoding="base64"`、`read_file` 只回文本而二进制必须 `download_file`。
2. **任务生命周期模块**：设计 Task/TaskStep 领域模型、两级状态机、`TaskStore` 抽象（内存实现），Manager 层 `RLock` 保证并发下 get→mutate→update 原子化。
3. **配置与存储重构**：`core/config.py` 用 pydantic-settings 替换失效的模块级常量；`StorageManager` 去掉构造即建 bucket 的副作用，改懒创建 + 幂等 + 404 白名单，boto3 异常统一包装为 `StorageError`。
4. **沙箱集成层**：`SandboxClient` 防腐层（路径解析、base64 二进制读写、header 鉴权、四类输出归一化、`shlex.quote` 安全删除）；`HermesHookEngine` 实现 OSS↔沙箱 pre/post 数据闭环。
5. **Agent 编排层**：Planner（`tool_choice` 强制 `create_plan`）、Executor（多轮 tool-calling + 消息历史回传 + `_assistant_message` 兼容真实 SDK 与 fake）、Orchestrator（生命周期 + 失败回滚）、LLMClient（懒构造）。
6. **工具层**：`BaseTool`/`ToolRegistry` 抽象 + `run_python`/`read_file`/`save_report` 三个具体工具 + `wiring.py` 组合根。
7. **API 层**：FastAPI 三端点，异步提交 + 轮询模式（先建任务拿 id、后台线程执行）。
8. **重试模块**：设计评审先行（07 号设计笔记）→ 实现 ErrorType 分类 + RetryPolicy 纯决策 + Executor 集成 + 8 字段观测事件。
9. **质量保障**：13 个测试文件 79 个用例全部离线可跑；真机端到端 demo（CSV→OSS→沙箱→Agent→结果 URL）；重试四场景确定性 demo。
10. **文档**：README + 8 篇 10 小节模板设计笔记（含“面试官最可能追问”章节）。

## 6. 技术难点

1. **二进制安全传输**：xlsx 等二进制文件进沙箱，UTF-8 解码必炸。需从 SDK 源码确认 `write_file(encoding="base64")` 与 `download_file` 字节流，用 base64 闭环。
2. **Jupyter 输出归一化**：SDK 返回四类 output（stream / execute_result / display_data / error），且“无输出”不等于“执行成功”，必须按 `status` 判断并完整归一化。
3. **多轮 tool-calling 闭环**：LLM 决定调工具 → 执行 → 把结果以 `role:"tool"` + `tool_call_id` 塞回消息历史 → 再让 LLM 决定；`message.content` 在有 tool_calls 时通常为 None，最终答案只在无 tool_calls 的那一轮。
4. **异步并发下的状态一致性**：多用户并发调用时任务状态可能被改坏，用状态机集中校验非法流转 + 双 `RLock`。
5. **重试分层决策**：Retry 放 Tool 内/Executor/Orchestrator 三层各有取舍，评审后定 Executor 层（单点统一策略 + 天然有 task/tool/attempt 上下文）；并解决“工具报错 ≠ 要重试”的分类难题。
6. **SDK 事实与 mock 的差异**：mock 通过、真机挂（响应多一层 `.data`）；SDK 无 api_key 构造参数；Python 版本与 PyPI 版本两个环境坑。
7. **全链路可测性**：让 79 个测试不依赖 Docker/LLM/网络——LLM、SDK、S3 全部接口化注入 fake。
8. **防无限循环与失败恢复**：`max_iterations` 上限；工具失败不直接判任务死刑，回传 LLM 换方案。

## 7. 真实 Bug

| # | Bug | 现象 | 来源 |
| --- | --- | --- | --- |
| 1 | 二进制文件 UTF-8 解码崩溃 | xlsx 上传沙箱必炸 | 原 `hermes_hooks.py` 用 `content.decode('utf-8')` |
| 2 | Jupyter 输出解析不完整 | 丢掉 execute_result/display_data；无输出时误判成功 | 原代码只处理 stream/error |
| 3 | import 即触网 | import 模块就实例化引擎/创建 bucket | 原 `agent_core.py` 模块级 `HermesHookEngine()`、`StorageManager.__init__` 建 bucket |
| 4 | `.env` 不生效 | python-dotenv 声明了却没用，配置散落为模块级常量 | 原 `config.py` |
| 5 | 真机响应多一层 `.data` | mock 用 `resp.status` 全绿，真机 SDK 返回 `resp.data.status`，执行结果全解析失败 | 提交 `4c7b04c`（真机验证发现） |
| 6 | 变量遮蔽 | `_normalize` 内 `data = out.data` 遮蔽外层响应，`status=data.status` 读到 `{}` | 提交 `4c7b04c` |
| 7 | head_bucket 404 判断不全 | MinIO 返回 `NoSuchBucket`/`NotFound` 而非 `"404"`，白名单外会误抛 | 提交 `c3fa137` 重构中修复 |
| 8 | boto3 异常裸抛 | 调用方无法区分网络/权限/bucket 根因 | 原 `storage_manager.py` |
| 9 | 单轮脚本无回传 | LLM 调用一次、工具结果从不回传，无法多步推理 | 原 `agent_core.py` 整体缺陷 |
| 10 | 鉴权缺失 | SDK 无 api_key 构造参数，服务完全开放 | 原 `hermes_hooks.py` |
| 11 | 工具失败一刀切 | 无错误分类，网络抖动与参数错误同样处理 | 重试设计评审发现的前置缺陷 |

## 8. 解决方案

| 问题 | 方案 | 落点 |
| --- | --- | --- |
| 二进制崩溃 | base64 编码 + `encoding="base64"` 写入；`download_file` 字节流合并读出 | `sandbox/client.py` `write_bytes_file` / `read_bytes_file` |
| 输出解析不完整 | `_normalize` 归一化四类输出到 `ExecutionResult`，成功判据用 `status` | `sandbox/client.py` |
| import 副作用 | 构造只建对象不发请求；bucket 懒创建；`get_settings()` 用 `lru_cache` | `storage/storage_manager.py`、`core/config.py` |
| 配置失效 | pydantic-settings 统一管理 `.env`/环境变量映射 | `core/config.py` |
| 真机 `.data` 差异 | `getattr(resp, "data", None)` 防御式解包，缺失时返回显式 error 而非崩溃 | `sandbox/client.py` `execute_python` |
| 变量遮蔽 | 局部变量改名 `out_data`，避免遮蔽外层响应 | `sandbox/client.py` `_normalize` |
| 404 判断 | `head_bucket` 404 code 白名单（404/NoSuchBucket/NotFound） | `storage/storage_manager.py` `ensure_bucket` |
| 异常裸抛 | 统一包装 `StorageError`，错误信息可执行 | `storage/storage_manager.py` |
| 单轮脚本 | Executor 多轮 tool-calling 循环 + 消息历史回传 + `max_iterations` 终止 | `agent/executor.py` |
| 鉴权缺失 | `X-AIO-API-Key` header 注入；docker-compose 支持 `SANDBOX_API_KEY` | `sandbox/client.py` `_build_client` |
| 失败一刀切 | `ErrorType` 6 类 + `retryable` 属性；RetryPolicy 纯决策只重试瞬态；8 字段观测事件 | `retry/retry_policy.py`、`tools/base_tool.py` |
| 状态并发破坏 | 两级状态机 `_ALLOWED_TRANSITIONS` + Manager/Store 双 `RLock` | `task/task_model.py`、`task/task_manager.py` |

## 9. 测试验证

**全量单测（本次核验实测）**：

```text
79 passed in 6.06s
```

- 13 个测试文件、1313 行测试代码，**全部离线**（fake LLM / fake SDK / fake S3，无需 Docker、无需网络、无需 API key）。

按模块覆盖：

| 模块 | 用例数 | 覆盖点 |
| --- | --- | --- |
| task 模型/管理 | 19 | 默认值、合法/非法流转、dict 往返、20 线程并发冒烟、原子性 |
| sandbox client/hooks | 15 | base64、字节流、四类输出、timeout、header、路径、删除引用、搬运闭环 |
| storage/config | 11 | 懒创建幂等、404 白名单、二进制往返、异常包装、配置缓存 |
| agent（planner/executor/orchestrator） | 9 | 计划解析、多轮循环、失败步骤、超限、生命周期 |
| tools/registry | 9 | 注册分发、schema 形状、成败映射 |
| retry | 12 | 分类、退避、抖动范围、四种场景、事件字段完整性 |
| api | 4 | 提交/轮询、404、列表、参数校验 |

**端到端真机验证**：

- `demo/run_demo.py`：示例 CSV → OSS → pre-hook 进沙箱 → Planner → Executor（真实 LLM 调 `run_python`/`save_report`）→ 结果 URL 回 OSS → 任务 SUCCESS。
- `demo/retry_demo.py`：脚本化 LLM + 真实 Executor，确定性展示四类路径：失败→重试→成功（attempts=2）、连续 3 次失败（max_attempts_exhausted）、参数错/权限拒绝不重试（attempts=1），`retry_events` 均正确写入 `task.metrics`。

## 10. 可量化指标

| 指标 | 数值 |
| --- | --- |
| 开发周期 | 2 天（2026-08-30 ~ 08-31），11 个提交 |
| 代码规模 | 60 个文件，约 4178 行（不含 venv/依赖） |
| 单测 | 79 个用例 / 13 个文件 / 1313 行测试代码，全部离线，6 秒跑完 |
| 模块规模（行） | task 410 · agent 407 · sandbox 224 · tools 230 · retry 95 · storage 99 · api 66 · 文档 702 |
| 工具 | 3 个可插拔工具（run_python / read_file / save_report） |
| 错误分类 | 6 类 ErrorType，2 类可重试（TRANSIENT / TIMEOUT） |
| 重试策略 | 默认 3 次尝试、指数退避（1s→2s，封顶 10s）、±50% 抖动 |
| 观测事件 | 每次工具尝试记录 8 字段（task_id/tool_name/attempt/error_type/error_message/retry_reason/duration_ms/final_status） |
| 循环防护 | tool-calling 上限 8 轮 |
| 设计文档 | 8 篇，统一 10 小节模板（含评审先行：07 评审 → 08 实现） |
| 并发安全 | 20 线程并发生命周期冒烟通过 |
| 部署 | docker-compose 一键起沙箱 + MinIO，镜像版本与官方推荐对齐 |

## 11. 面试故事

**故事一：mock 全绿、真机全挂（最值得讲）**

写完沙箱封装后单测全过，结果真机端到端跑起来发现所有执行结果都解析失败。对比真机响应发现 SDK 返回多了一层 `.data`，而 fake mock 没这层。修复时没有简单改 mock，而是做防御式解包 `getattr(resp, "data", None)` + 缺失时返回显式 error，并补了“kernel died / data=None”的测试。同时排查出 `_normalize` 里的变量遮蔽（`data = out.data` 把外层响应盖掉了）。**教训：mock 要与真机结构对齐，防腐层要做防御式解包。**

**故事二：xlsx 必炸的二进制传输**

接手时代码 `content.decode('utf-8')` 写文件，文档 Agent 处理 xlsx 直接崩。我没有想当然，而是直接读 SDK 源码确认 `write_file` 的 `encoding` 支持 `base64`，且二进制读出必须走 `download_file`（`read_file` 只回文本）。最终方案是统一 base64 进、字节流出，pre/post hook 形成 OSS↔沙箱闭环。**教训：能力边界要核实源码，而不是猜 API。**

**故事三：重试放哪一层，先评审后动手**

做重试前先写了设计评审（07 号笔记），对比 Tool 内 / Executor / Orchestrator 三层：放 Tool 内每个工具重复实现难统一观测；放 Orchestrator 粒度太粗等于重跑整个计划；最后定 Executor 内层循环，配合 `ErrorType` 分类器——只重试瞬态故障（网络/429/超时/冷启动），参数错、权限、代码 bug 立刻失败。退避用指数 + 抖动防重试风暴，8 字段观测事件直接进 `task.metrics`。**面试可主动回答：工具报错 ≠ 要重试，判断标准是“原样重发有多大可能成功”。**

**故事四：从 demo 脚本到可上线形态的重构**

核心不只是加了功能，而是修了三类“架构债”：import 副作用（构造即触网）、状态不可追踪（单轮跑完即丢）、硬编码不可测（工具写死在 main）。改法：`get_settings()` 单例 + bucket 懒创建、两级状态机 + `TaskStore` 抽象、`BaseTool` + 组合根 + 全 fake 注入，让 79 个测试 6 秒离线跑完。

**追问应对要点**：

- 为什么状态机？→ 异步并发下防脏状态，合法流转集中一处校验。
- 为什么 retry 放 Executor？→ 单点统一策略 + 有 task/tool/attempt 上下文，工具保持无状态可测。
- plan 步骤与 task.steps 什么关系？→ MVP 中 plan 是执行上下文、task.steps 是实际执行轨迹，后续加 plan_id 对齐。
- 内存存储怎么演进？→ `TaskStore` ABC 已预留，换 Redis 实现不动调用方；后台线程换 Celery。
- 工具结果太大怎么办？→ 截断/摘要回传，或让工具返回 OSS key 引用而非全文。

## 12. 当前不足

1. **存储与队列**：`InMemoryTaskStore` + 后台 `threading.Thread`，进程重启丢任务/丢 in-flight 任务 → 需 Redis + 持久化队列（`TaskStore` ABC 已预留）。
2. **无权限/安全层**：`run_python` 执行任意代码目前只依赖沙箱隔离，缺命令/文件/危险操作白名单（`security/` 未做）。
3. **无评估体系**：`Task.metrics` 预留但未落库统计成功率、耗时、Token 消耗（`evaluation/` 未做；`response.usage` 也未累加）。
4. **文档解析核心能力未接入**：markitdown MCP 未接入，当前 `read_file` 只能读文本，PDF/Excel/PPTX 解析是 Phase 2 的 `mcp_adapter` 工作。
5. **无 Memory**：短期任务上下文、长期历史任务记忆均未实现。
6. **重试覆盖不全**：只有工具层重试；LLM 调用本身无重试；`SandboxError`/`StorageError` 默认归为 BUSINESS（保守取舍，后续应自带 `error_type`）。
7. **上下文风险**：工具结果直接全文回传，无截断/摘要，大输出可能撑爆上下文。
8. **顺序执行**：同一轮多个工具调用是顺序执行，未并行（需并发 + 按 call id 对齐结果）。
9. **文档与代码不一致**：README 1.3 仍列“待补齐”三项（卷持久化/API key/host-gateway），实际 docker-compose 已实现，文档未更新。
10. **遗留 shim**：根目录 `config.py` / `storage_manager.py` / `hermes_hooks.py` / `agent_core.py` 是兼容层，迁移完成后待删除。
11. **工程化缺口**：无 CI、无 coverage、无 lint；沙箱 API key 留空时服务默认开放；base64 传输 +33% 体积，超大文件需改流式上传。
