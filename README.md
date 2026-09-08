# Document Specialist Agent

面向文档处理与代码执行的单机 Agent Runtime 学习原型，基于 AIO Sandbox、SQLite、MinIO/S3 和 OpenAI 兼容模型接口。

**先读 [项目学习指南](docs/LEARNING_GUIDE.md)**：按一次任务的执行顺序讲解代码，附运行命令、对应测试和小练习。

## 当前具备什么

- Orchestrator / Planner / Executor 三层架构，结构化计划与多轮 Tool Calling。
- 任务、步骤、计划、工具调用、重试及消息轨迹持久化；SQLite 队列和固定 worker。
- 可插拔工具、Schema 校验、权限声明、分类重试、轮数与调用预算。
- 文件输入、任务专属目录和对象前缀；CSV/XLSX/TXT/MD/JSON 解析与产物校验。
- 短期上下文预算、大结果引用；显式选择保存的脱敏长期笔记与关键词检索。
- 运行指标、固定离线评估集；可选 MCP 解析服务与客户端。

完整能力与限制见 [能力矩阵](PROJECT_CAPABILITY_MATRIX.md)，真实验证证据见 [学习版验证记录](docs/verification/02_learning_runtime.md)。

**已知沙箱超时后执行可能继续（SEC-001）按用户决定延期**，不影响先学习其他模块，但最终安全验收尚未通过。学习版功能完成不等于生产安全认证。当前 API 无登录鉴权，只在本机使用；共享沙箱不提供恶意多租户隔离。

## 快速开始

建议 Python 3.12 或 3.13。Windows PowerShell，已有虚拟环境可跳过创建：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m demo.learning_demo
.\.venv\Scripts\python.exe -m evaluation.run
```

Linux/macOS 使用 `.venv/bin/python`。学习 demo 无需模型密钥或 Docker：真实运行状态机、SQLite、文件校验和 Agent Loop，外部服务采用固定测试替身，不在宿主机执行生成代码。

## 真实运行

根据 `.env.example` 补充自己的 `.env`，不要覆盖已有密钥。确认 ALLOWED_TOOLS 包含 parse_document 和 read_step_output。启动沙箱和 MinIO 后运行：

```powershell
docker compose up -d
.\.venv\Scripts\python.exe -m demo.run_demo
# 或启动本机 API
.\.venv\Scripts\python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8000
```

浏览器访问 http://127.0.0.1:8000/docs，通过交互页面提交任务。真实运行需要 LLM_API_KEY，并产生供应商调用费用。

| 接口 | 用途 |
| --- | --- |
| POST /tasks | 提交请求、可选 Base64 文件和产物要求，落盘后返回 ID |
| GET /tasks/{id} | 查询状态、步骤、结果和轨迹 |
| GET /tasks | 本地任务历史 |
| GET /evaluation | 累计运行指标 |
| DELETE /memory/{task_id} | 删除长期笔记，保留任务轨迹 |

输入、API 请求示例、故障恢复和可选 MCP 的命令见学习指南。

## 主要目录

| 目录 | 职责 |
| --- | --- |
| agent | 规划、执行循环、任务编排、调用预算 |
| task | 状态模型、SQLite 存储、队列 worker、显式中断恢复 |
| tools / security / retry | 工具接口与分发、权限检查、重试决策 |
| sandbox / storage | AIO Sandbox 与 MinIO/S3 适配器 |
| documents | 输入检查、文档解析、产物要求校验 |
| memory | 上下文裁剪与可复用笔记 |
| evaluation | 指标汇总和固定评估集 |
| api / demo / tests | 接口、演示与测试 |

`agent/wiring.py` 是依赖组装入口。根目录早期脚本及旧阶段 Design Note 保留作历史参考；学习当前版本从本 README 链接开始。

## 验证与边界

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m demo.learning_demo
.\.venv\Scripts\python.exe -m evaluation.run
# 可选 MCP 实际协议测试
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
.\.venv\Scripts\python.exe -m pytest tests/test_mcp_parser.py -q
# 有 Docker/MinIO 时单独运行；不涉及已延期的超时探针
.\.venv\Scripts\python.exe -m demo.artifact_smoke
```

GitHub Actions 运行完整依赖下的测试与离线演示。真实 LLM、沙箱和对象存储的联合验收单独记录，不能用 fake 测试替代。

数据库默认 `data/tasks.sqlite3`；一次任务快照是一行 JSON，适合小规模学习。没有分页、分布式租约或自动重放任意代码。突然中断的 RUNNING 任务必须在所有 worker 停止后显式标记失败，再确认执行环境；详情见学习指南。
