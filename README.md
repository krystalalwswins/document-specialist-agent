# Document Specialist Agent

面向文档处理与代码执行的 Agent Runtime 原型 —— 基于 [agent-infra/sandbox](https://github.com/agent-infra/sandbox)（AIO Sandbox）二次开发。

一句话定位：从“能跑通的 demo 脚本”升级为**有任务生命周期、工具注册表、计划-执行循环的 Agent 运行时**；沙箱作为隔离执行层，对象存储作为结果层。

当前交付依据：[简历对标与五批计划](docs/DELIVERY_PLAN.md)。第一批 Retry/Security 的代码与离线验证已完成，真实 Docker 验收待完成；详见 [验证记录](docs/verification/01_security.md) 和 [Design Note](docs/design/10_security_module.md)。

开发定位：单用户本地原型。当前共享容器/文件系统、内存任务记录、未鉴权 API；尚未完成恶意多租户隔离和持久化执行轨迹。

---

## 1. 上游项目调研

> 调研时间：2026-08-30。依据官方 GitHub README、`sdk/python` 官方说明与官方文档站直接核实，非二手资料。

### 1.1 项目定位

| 项 | 内容 |
| --- | --- |
| 仓库 | https://github.com/agent-infra/sandbox |
| 定位 | AI Agent 一体化沙箱：Browser / Shell / File / VSCode / Jupyter / MCP 聚合在单个 Docker 容器，共享文件系统 |
| 本质 | Agent Infra 层，不是业务 Agent（这正是本项目要补的层） |
| License | Apache-2.0 |
| SDK | Python: `agent-sandbox`；TS: `@agent-infra/sandbox`；Go: `sandbox-sdk-go` |

### 1.2 已核实的 SDK 事实

- Python SDK 官方要求 **Python 3.8+**；本机默认 base 环境是 3.7.0，不满足，已迁移。
- 环境决策（2026-08-30 实测）：使用 **Python 3.13.2**（`C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe`）创建项目 venv（`.venv`）；3.11.9 作为兼容性回退。
- 当前适配并锁定 `agent-sandbox==0.0.30`，接口以该版本为准。
- 客户端形态：`Sandbox`（同步）与 `AsyncSandbox`（异步）两种。
- 当前代码使用的 API 与官方 README 一致，未用过时接口：

| 能力 | 官方 API |
| --- | --- |
| 环境信息 | `client.sandbox.get_context().home_dir` |
| Shell | `client.shell.exec_command()` |
| 文件 | `client.file.read_file() / write_file()` |
| Python 执行 | `client.jupyter.execute_code()` |
| Node.js 执行 | `client.nodejs.execute_nodejs_code()` |
| HTTP API | `/v1/sandbox`、`/v1/shell/exec`、`/v1/file/read`、`/v1/file/write`、`/v1/browser/screenshot`、`/v1/jupyter/execute` |

### 1.3 部署对齐（docker-compose）

本项目 docker-compose 使用的镜像 `enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0` 是官方对中国大陆用户推荐的 pin 版本；`seccomp:unconfined`、`shm_size: 2gb`、`127.0.0.1:8080` 端口绑定均与官方一致。

当前 compose 已配置工作区卷、`SANDBOX_API_KEY` 环境变量和 host-gateway。API key 留空仍不开启鉴权，使用时需自行设置。

本批增加 CPU、总内存、swap 和 PID 限制，MinIO 端口绑定本机。`shm_size` 只配置共享内存，不代表总内存上限。配置实际生效需运行 `python -m demo.security_smoke` 验证。

保留上游的 `seccomp:unconfined`，这是开发容器，不能据此宣称完全抵御恶意代码或限制其所有网络访问。

### 1.4 可复用能力（对文档处理定位）

官方预置 MCP server：`browser` / `file` / `shell` / **`markitdown`**（`convert` / `extract_text` / `extract_images`）。

markitdown 是文档处理 Agent 的核心能力来源，计划在 Phase 2 通过 MCP Tool Adapter 接入本项目的 Tool Registry，而不是让 LLM 手写文档解析代码。

### 1.5 二进制文件方案（已实测确认）

直接读 SDK 源码确认：`file.write_file` 的 `encoding` 参数支持 `utf-8 / base64 / raw`，二进制文件统一 base64 写入；`file.download_file` 以字节流读出。另有 `str_replace_editor` 内置 Excel/PDF/PPTX 查看能力。方案 A（base64）为官方能力，无需 curl 下载。

---

## 2. 当前代码评估摘要

当前代码已具备任务/步骤状态机、三层编排、多轮 Tool Calling、三个可插拔工具、二进制传输和工具重试。

第一批补齐异常分类、重放安全声明、参数校验、工具/路径权限、拒绝审计、执行超时上限和独立 Jupyter 会话清理，同时修正了 SDK 响应 envelope 解析。任务持久化、产物达标校验和 Memory/Evaluation 仍按交付计划推进。

Python 每次调用使用新 session，跨调用状态请保存为工作区文件。超时或执行状态不确定时终止当前任务，不让模型继续重放代码。独立 session 不隔离共享文件系统。

---

## 3. 目标架构

```text
                 User
                  |
                  |
             API Layer
                  |
                  |
          Agent Orchestrator
                  |
        --------------------
        |        |         |
     Planner   Memory    Tool Registry
        |
        |
    Task Executor
        |
        |
   Sandbox Runtime
        |
        |
 Docker Container
        |
        |
 Result Storage
```

---

## 4. Phase 1 目录规划

```text
document-specialist-agent/
├── agent/                  # Orchestrator + Planner + Executor
│   ├── orchestrator.py
│   ├── planner.py
│   └── executor.py
├── task/                   # 任务生命周期
│   ├── task_model.py
│   └── task_manager.py
├── tools/                  # 工具注册表
│   ├── base_tool.py
│   ├── tool_registry.py
│   ├── sandbox_tool.py
│   ├── file_tool.py
│   └── report_tool.py
├── sandbox/                # 由 hermes_hooks.py 拆分重构
│   ├── client.py
│   └── hooks.py
├── storage/                # 由 storage_manager.py 迁移
│   └── storage_manager.py
├── core/                   # 由 config.py 迁移
│   └── config.py
├── demo/                   # 原 agent_core.py main 降级为 demo 脚本
├── tests/
├── docker-compose.yaml
├── requirements.txt
└── README.md
```

---

## 5. 开发路线

### Phase 1 —— MVP（当前）

1. 环境与骨架：Python 3.13 venv、git init、目录结构、README
2. ✅ `core/config.py` + Storage 重构：去 import 副作用、lazy bucket、`.env` 加载（根目录 `config.py` / `storage_manager.py` 已降级为兼容 shim，待迁移后删除）
3. ✅ `task/`：Task 模型 + TaskManager（内存实现 + 存储接口抽象，后续可换 Redis）
4. ✅ `tools/`：BaseTool + ToolRegistry + sandbox/file/report 具体工具
5. ✅ `sandbox/`：Hook 重构、二进制安全传输（base64）、统一输出解析
6. ✅ `agent/`：Planner（LLM 结构化计划）→ Executor（多轮 tool-calling 循环）→ Orchestrator
7. ✅ 最小 API：FastAPI `POST /tasks`、`GET /tasks/{id}`

### Phase 2 —— 工程增强

- `memory/`：短期（任务上下文）+ 长期（历史任务），Redis
- ✅ `retry/`：工具失败自动重试策略（ErrorType + RetryPolicy + Executor 集成）
- `security/`：工具/文件/对象前缀权限已实现并离线验证；真实沙箱验收待完成，不提供任意 Python 命令黑名单保证
- `evaluation/`：成功率、工具调用次数、耗时、Token 消耗
- `tools/mcp_adapter.py`：接入沙箱预置 MCP server（markitdown / file / shell）

---

## 6. 快速开始

```powershell
# 1. 创建虚拟环境（首次）
& 'C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe' -m venv .venv

# 2. 安装依赖（首次；若默认源失败，追加 -i https://pypi.org/simple）
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. 启动本地服务（沙箱 + MinIO）
docker compose up -d

# 4. 激活环境（每个新终端）
.venv\Scripts\Activate.ps1

# 5. 运行测试
python -m pytest

# 6. 端到端 Demo（需先设置 .env 的 LLM_API_KEY，并 docker compose up -d）
python -m demo.run_demo

# 7. 启动 API（另开终端；任务执行时才真正用到 LLM_API_KEY）
.venv\Scripts\python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8000
```

---

## 7. 设计笔记索引

每个模块交付时附 Design Note，统一按 [TEMPLATE.md](docs/design/TEMPLATE.md) 的 10 小节模板编写，归档于 `docs/design/`。

- [01_task_module.md](docs/design/01_task_module.md)：任务生命周期模块
- [02_config_and_storage.md](docs/design/02_config_and_storage.md)：配置与对象存储模块
- [03_sandbox_module.md](docs/design/03_sandbox_module.md)：沙箱集成层（SDK 封装 + 二进制安全 Hook）
- [04_agent_module.md](docs/design/04_agent_module.md)：Agent 编排层（Planner / Executor / Orchestrator + tools 抽象）
- [05_tools_integration.md](docs/design/05_tools_integration.md)：具体工具与组装（sandbox/file/report + wiring + demo）
- [06_api_layer.md](docs/design/06_api_layer.md)：最小 API 层（FastAPI + 后台执行）

- [07_retry_design_review.md](docs/design/07_retry_design_review.md)：历史重试评审
- [08_retry_module.md](docs/design/08_retry_module.md)：历史重试实现
- [09_security_design_review.md](docs/design/09_security_design_review.md)：第一批设计评审
- [10_security_module.md](docs/design/10_security_module.md)：第一批实现与面试说明

## 8. 第一批验证

```bash
python -m pytest -q
python -m demo.retry_demo
python -m demo.security_demo
# 需启动 Docker 沙箱，不需要 LLM key；退出 2 表示未验证
python -m demo.security_smoke
```

工作区路径检查同时用于 Hook 和工具；生产装配可通过 `.env` 中的 JSON 数组 `ALLOWED_TOOLS` / `ALLOWED_PERMISSIONS` 控制模型可用工具。新增工具默认不自动重试，只有可安全重放时才声明 `retry_safe = True`。
