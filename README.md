# Document Specialist Agent

企业级智能任务执行 Agent 平台 —— 基于 [agent-infra/sandbox](https://github.com/agent-infra/sandbox)（AIO Sandbox）二次开发。

一句话定位：从“能跑通的 demo 脚本”升级为**有任务生命周期、工具注册表、计划-执行循环的 Agent 运行时**；沙箱作为隔离执行层，对象存储作为结果层。

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
- PyPI 上 `agent-sandbox` 最新版本为 **0.0.30**（原 requirements 中的 `>=0.1.0` 不存在，已修正为 `>=0.0.30`）。
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

与官方 compose 相比，本项目待补齐：

1. `volumes: sandbox_data:/home/gem/workspace` —— 沙箱工作区持久化（当前容器重启即丢状态）
2. `SANDBOX_API_KEY` —— 官方推荐开启，保护 API / JupyterLab / VNC（当前完全开放）
3. `extra_hosts: host.docker.internal:host-gateway` —— 沙箱内访问宿主机（MinIO 互通需要）

### 1.4 可复用能力（对文档处理定位）

官方预置 MCP server：`browser` / `file` / `shell` / **`markitdown`**（`convert` / `extract_text` / `extract_images`）。

markitdown 是文档处理 Agent 的核心能力来源，计划在 Phase 2 通过 MCP Tool Adapter 接入本项目的 Tool Registry，而不是让 LLM 手写文档解析代码。

### 1.5 待实测项

SDK 二进制写文件能力（xlsx 入沙箱）官方文档未明确，候选方案：

- A：base64 文本写入 + 沙箱内解码
- B：OSS presigned URL + 沙箱内下载

实现 sandbox 模块时先跑最小验证脚本再定方案。

---

## 2. 当前代码评估摘要

（基于 2026-08-30 实际代码审查）

- 现状：单轮脚本 —— LLM 调用一次、工具结果不回传，无法多步推理；无任务生命周期、无工具注册表、无重试/权限/观测。
- 主要问题：import 即触发存储初始化；文件传输按 UTF-8 解码，xlsx 必炸；沙箱输出解析不完整；print 日志无任务维度。
- 保留价值：OSS ↔ Sandbox ↔ LLM 垂直链路真实可演示；Pre/Post Hook 设计正确；docker-compose 本地一键环境。

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
2. `core/config.py` + Storage 重构：去 import 副作用、lazy bucket、`.env` 加载
3. ✅ `task/`：Task 模型 + TaskManager（内存实现 + 存储接口抽象，后续可换 Redis）
4. `tools/`：BaseTool + ToolRegistry + sandbox/file/report 工具
5. `sandbox/`：Hook 重构、二进制安全传输、统一输出解析
6. `agent/`：Planner（LLM 结构化计划）→ Executor（多轮 tool-calling 循环）→ Orchestrator
7. 最小 API：FastAPI `POST /tasks`、`GET /tasks/{id}`

### Phase 2 —— 工程增强

- `memory/`：短期（任务上下文）+ 长期（历史任务），Redis
- `retry/`：工具失败自动重试策略
- `security/`：工具/文件/危险命令权限控制
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
```

---

## 7. 设计笔记索引

每个模块交付时附 Design Note（模块作用 / 设计原因 / 核心流程 / 关键代码 / 面试回答），归档于 `docs/design/`。

- [01_task_module.md](docs/design/01_task_module.md)：任务生命周期模块
