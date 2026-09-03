# PROJECT PROGRESS —— 真实进度记录

> 更新：2026-09-01
> 状态图例：**DONE**（已实现并验证）/ **IN_PROGRESS**（正在实现）/ **PLANNED**（尚未实现）/ **DEFERRED**（暂时不做）

---

## 0. 验证基线（真实可核验）

| 项 | 结果 | 时间 |
| --- | --- | --- |
| 单元测试 | `pytest -q` → **79 passed in 5.92s** | 2026-09-01 复核 |
| 测试形态 | 13 个测试文件，全部离线（fake LLM / SDK / S3） | 持续有效 |
| 端到端 demo | `demo/run_demo.py` 设计为真机链路（需 docker compose + LLM_API_KEY） | 环境就绪时可跑 |
| 重试 demo | `demo/retry_demo.py` 离线确定性验证（4 场景） | 已实现 |
| 代码规模 | 60 文件 / 约 4178 行（不含 venv/.git） | 2026-08-31 统计 |

---

## 1. 总览

| # | 模块 | 状态 |
| --- | --- | --- |
| 1 | 项目骨架与环境（Python 3.13 venv、目录、docker-compose、git） | DONE |
| 2 | 配置管理 `core/config.py` | DONE |
| 3 | 对象存储 `storage/`（MinIO/S3） | DONE |
| 4 | 任务生命周期 `task/` | DONE |
| 5 | 沙箱集成 `sandbox/` | DONE |
| 6 | Agent 编排 `agent/`（Planner/Executor/Orchestrator） | DONE |
| 7 | 工具抽象与注册表 `tools/` | DONE |
| 8 | 三个具体工具（run_python / read_file / save_report） | DONE |
| 9 | HTTP API `api/` | DONE |
| 10 | 工具层重试 `retry/` | DONE |
| 11 | 测试体系（79 用例离线） | DONE |
| 12 | 端到端 Demo + 重试 Demo | DONE |
| 13 | 设计文档（8 篇 10 小节模板） | DONE |
| 14 | 三份策略文档（Final Spec / Progress / Resume Material） | DONE（本文档） |
| 15 | 文档与代码同步（README 1.3"待补齐"描述已过期） | IN_PROGRESS |
| 16 | Permission / Security 层 | PLANNED |
| 17 | Short-term Memory（上下文/截断/摘要） | PLANNED |
| 18 | Result Validator + 任务级 Recovery | PLANNED |
| 19 | Redis TaskStore + 持久化任务队列 | PLANNED |
| 20 | MCP / markitdown 文档解析工具 | PLANNED |
| 21 | LLM 调用重试 + usage/token 统计 | PLANNED |
| 22 | Evaluation 指标落库 | PLANNED |
| 23 | Long-term Memory | PLANNED |
| 24 | 并行工具调用 | PLANNED |
| 25 | Observability 增强（事件落库 / 可选 trace） | PLANNED |
| 26 | CI（自动 pytest + coverage） | PLANNED |
| 27 | Multi-Agent / Workflow 引擎 | DEFERRED |
| 28 | 生产部署（多实例 / 横向扩展） | DEFERRED |
| 29 | 超大文件流式传输 | DEFERRED |
| 30 | 多租户 / 账号体系 | DEFERRED |

---

## 2. DONE —— 已实现并验证

### 2.1 项目骨架与环境
- Python 3.13.2 venv、`requirements.txt`、`pytest.ini`、docker-compose（sandbox v1.11.0 + MinIO）。
- 证据：README 快速开始可复现；`git log` 11 个提交。

### 2.2 配置管理（core/config.py）
- pydantic-settings + `.env` + `get_settings()` 单例；import 无副作用。
- 证据：`tests/test_config.py`（3 用例）。

### 2.3 对象存储（storage/storage_manager.py）
- MinIO/S3 封装：懒创建 bucket、presigned URL、`StorageError` 包装、可注入 fake。
- 证据：`tests/test_storage_manager.py`（8 用例，含二进制往返、404 白名单、幂等）。

### 2.4 任务生命周期（task/）
- Task/TaskStep 两级状态机、`TaskStore` ABC + 内存实现、`TaskManager` 门面、双 RLock、序列化契约。
- 证据：`tests/test_task_model.py` + `test_task_manager.py`（19 用例，含 20 线程并发冒烟）。

### 2.5 沙箱集成（sandbox/）
- `SandboxClient` 防腐层：base64 二进制安全、四类输出归一化、header 鉴权、`shlex.quote` 删除；`HermesHookEngine` pre/post 数据闭环。
- 证据：`tests/test_sandbox_client.py` + `test_sandbox_hooks.py`（15 用例）。

### 2.6 Agent 编排（agent/）
- Planner（`create_plan` 强制结构化）、Executor（多轮 tool-calling + `max_iterations=8` + 失败不中断）、Orchestrator（生命周期 + 失败回滚）、LLMClient、wiring 组合根。
- 证据：`tests/test_planner.py` / `test_executor.py` / `test_orchestrator.py`（9 用例）。

### 2.7 工具抽象与注册表（tools/）
- `BaseTool` ABC + `ToolRegistry` 注册/分发/schema 生成。
- 证据：`tests/test_tool_registry.py`（5 用例）。

### 2.8 三个具体工具
- `run_python`（沙箱执行）、`read_file`（文本读取）、`save_report`（OSS 落盘 + URL）。
- 证据：`tests/test_concrete_tools.py`（4 用例）。

### 2.9 HTTP API（api/）
- `POST /tasks`、`GET /tasks/{id}`、`GET /tasks`；后台线程异步执行；工厂可注入。
- 证据：`tests/test_api.py`（4 用例，TestClient + fake orchestrator）。

### 2.10 工具层重试（retry/）
- `ErrorType` 6 类分类、`RetryPolicy` 纯决策（指数退避 + 抖动）、Executor `_invoke_tool` 集成、8 字段 `retry_events`。
- 证据：`tests/test_retry.py`（12 用例）+ `demo/retry_demo.py`（4 场景确定性输出）。

### 2.11 测试体系
- 13 个文件 / 79 用例，全部离线；2026-09-01 复核 79 passed in 5.92s。

### 2.12 端到端 Demo
- `demo/run_demo.py`：CSV → OSS → pre-hook → 沙箱 → Agent → save_report → URL → SUCCESS。
- 运行前提：docker compose up -d + `.env` 配置 LLM_API_KEY；环境就绪时验证。

### 2.13 设计文档
- `docs/design/` 8 篇（01~08，含 07 评审先行 → 08 实现记录）。

---

## 3. IN_PROGRESS —— 正在实现

| 项 | 现状 | 待办 |
| --- | --- | --- |
| 文档与代码同步 | README 1.3 仍写"待补齐"三项（卷持久化/API key/host-gateway），实际 docker-compose 已实现 | 更新 README；迁移根目录遗留 shim（config.py / storage_manager.py / hermes_hooks.py / agent_core.py）并删除 |
| 项目策略落地 | 本轮交付 Final Spec / Progress / Resume Material | 按优先级把 PLANNED 模块转 DONE（见第 4 节） |

---

## 4. PLANNED —— 规划中（按优先级）

> 每个模块"转 DONE"的验收标准统一为：**代码合入 + 对应单测/集成测试通过 + （如适用）真机或 demo 验证**。

### P0 —— 简历核心，面试前必须真实完成

| 模块 | 目标 | 依赖 | 验收标准 |
| --- | --- | --- | --- |
| Permission / Security 层 | 工具/文件/命令白名单 + 拒绝审计 | Registry 接口稳定 | 白名单拦截有测试；危险操作（删除/外发）需审批或显式放行 |
| Short-term Memory | 上下文组装 + 工具大结果截断/摘要/OSS 引用 | Executor 消息组装点 | 大输出任务不超 token 上限，有截断策略测试 |
| Result Validator + 任务级 Recovery | 校验产物存在/非空；失败重试或重规划 | Task 状态机 + Orchestrator | 校验失败可触发恢复路径，有集成测试 |
| Redis TaskStore + 队列 | 进程重启不丢任务；API 提交进队列 | TaskStore ABC 已就绪 | Redis 版 store 通过同一套生命周期测试；重启恢复验证 |
| MCP / markitdown 文档解析工具 | 接入沙箱预置 MCP，支持 PDF/Excel/PPTX → Markdown | sandbox MCP 能力 | 至少一种真实文档解析成功并有测试 |
| LLM 调用重试 + usage/token 统计 | 复用 RetryPolicy；`response.usage` 累加进 metrics | RetryPolicy 已就绪 | LLM 瞬态失败可恢复；metrics 有 token 字段 |
| Evaluation 指标落库 | 成功率/耗时/token/重试率/失败原因分布 | 上述模块事件源 | 固定 Eval 集可复跑并输出报告 |

### P1 —— 增强

| 模块 | 目标 | 验收标准 |
| --- | --- | --- |
| Long-term Memory | 任务摘要入库 + 检索注入 | 同类任务可命中历史知识，有检索测试 |
| 并行工具调用 | 同轮多工具并发执行 + 按 call id 对齐 | 并发测试通过，结果顺序稳定 |
| Observability 增强 | retry_events/metrics 落库 + task_id 维度日志 | 事件可查询；日志带 task_id |
| CI | GitHub Actions 自动 pytest + coverage | PR 自动跑测试，coverage 有基线 |

---

## 5. DEFERRED —— 暂时不做

| 模块 | 原因 |
| --- | --- |
| Multi-Agent / Workflow 引擎 | 单 Agent 平台已覆盖简历核心；后续可选 |
| 生产部署（多实例/横向扩展） | 秋招窗口内无真实生产流量 |
| 超大文件流式传输 | 当前 base64 方案满足演示与面试验证 |
| 多租户 / 账号体系 | 超出"Agent 运行时"定位，属产品层 |

---

## 6. 状态判定规则

- **DONE**：必须同时满足"代码存在 + 测试通过 +（适用时）真机/demo 验证"。缺测试的代码只能算 IN_PROGRESS。
- **PLANNED**：尚未写代码；可作为最终设计能力讨论，但不能在简历中写成已实现。
- **DEFERRED**：明确不做或不承诺；简历中不出现。
