# PROJECT CAPABILITY MATRIX —— Document Specialist Agent

> 2026-09-07 更新：以下早期能力统计保留作历史参考。最新能力状态、真实验证证据和限制见 [第一批验证记录](docs/verification/01_security.md)，后续范围以 [交付计划](docs/DELIVERY_PLAN.md) 为准。Security 代码与离线验证已完成，真实沙箱验收待完成。

> 一句话定位：从“能跑通的 demo 脚本”升级为**有任务生命周期、工具注册表、计划-执行循环的 Agent 运行时**；沙箱作为隔离执行层，对象存储作为结果层。
>
> 基于 [agent-infra/sandbox](https://github.com/agent-infra/sandbox)（AIO Sandbox，v1.11.0 / Python SDK 0.0.30）二次开发。

---

## 1. 总体能力矩阵

| 能力域 | 能力项 | 状态 | 实现位置 | 验证方式 | 面试可讲点 |
| --- | --- | --- | --- | --- | --- |
| 任务生命周期 | Task/TaskStep 领域模型 + 状态机 | ✅ | `task/task_model.py` | 11 个单测（合法/非法流转、序列化往返） | 非法流转抛错，防并发脏状态 |
| 并发安全 | Manager 层 `RLock` + Store 层 `RLock` | ✅ | `task/task_manager.py` | 20 线程并发冒烟测试 | get→mutate→update 原子化 |
| 存储抽象 | `TaskStore` ABC，可换 Redis/DB | ✅ | `task/task_manager.py` | 自定义 store 注入测试 | 面向接口，Phase 2 换 Redis 不动调用方 |
| 配置管理 | pydantic-settings + `.env` + 单例 | ✅ | `core/config.py` | 3 个单测（默认值/覆盖/缓存） | `lru_cache` 单例、`extra=ignore` |
| 对象存储 | MinIO/S3 封装：懒创建 bucket、presigned URL、错误包装 | ✅ | `storage/storage_manager.py` | 8 个单测（fake S3，全离线） | 构造不触网；404 白名单；`StorageError` 包装 |
| 沙箱集成 | SDK 防腐层：路径解析、二进制 base64、header 鉴权 | ✅ | `sandbox/client.py` | 12 个单测（fake SDK） | 文本/二进制走不同 API（read_file vs download_file） |
| 输出归一化 | Jupyter 四类输出（stream/execute_result/display_data/error） | ✅ | `sandbox/client.py` `_normalize` | 单测覆盖四类 + timeout + data 缺失 | “无输出≠成功”，以 `status` 为准 |
| 数据搬运 Hook | OSS→沙箱（pre）+ 沙箱→OSS 并清洗（post） | ✅ | `sandbox/hooks.py` | 3 个单测（fake 搬运/清洗） | 执行在沙箱、成果在 OSS 的边界 |
| 安全删除 | `shlex.quote` 防注入的 `rm -f` | ✅ | `sandbox/client.py` | 单测验证带空格路径 | 沙箱是 Linux，路径必须 shell-safe |
| LLM 接入 | OpenAI 兼容客户端，懒构造、可注入 | ✅ | `agent/llm_client.py` | 单测用 fake LLM | 无 key 可组装，首次 chat 才校验 |
| 规划 | Planner 用 `create_plan` 函数调用强制结构化计划 | ✅ | `agent/planner.py` | 3 个单测（解析/缺计划/空计划） | `tool_choice` 强制，避免自由文本 |
| 多轮执行 | Executor tool-calling 循环，结果回传 LLM | ✅ | `agent/executor.py` `run` | 3 个单测 + retry 集成测试 | 真 Agent 核心：LLM 决定→执行→回传→再决定 |
| 循环防护 | `max_iterations=8` 上限 | ✅ | `agent/executor.py` | 单测验证超限抛错 | 防 LLM 无限调工具 |
| 失败不中断 | 单步失败记 FAILED 后继续，交还 LLM 决策 | ✅ | `agent/executor.py` | 单测验证失败步骤+继续执行 | 工具失败≠任务失败 |
| 编排 | Orchestrator 串起生命周期，失败标记+回滚 | ✅ | `agent/orchestrator.py` | 3 个单测（成功/失败/规划失败） | 非终态才 fail_task，不覆盖已成功任务 |
| 工具抽象 | `BaseTool`：schema 自描述 + 执行分离 | ✅ | `tools/base_tool.py` | registry 单测 | 新工具只加一个类 |
| 工具注册表 | 注册/查询/批量 schema/分发 | ✅ | `tools/tool_registry.py` | 5 个单测 | Executor 只认 Registry 接口，不认具体工具 |
| 具体工具 | run_python / read_file / save_report | ✅ | `tools/sandbox_tool.py` `file_tool.py` `report_tool.py` | 4 个单测（fake 沙箱/存储） | save_report 是“成果落盘”唯一出口 |
| 组合根 | `build_orchestrator()` 依赖装配一处完成 | ✅ | `agent/wiring.py` | 无网络可组装 | 业务类保持纯逻辑、可注入、可测 |
| HTTP API | `POST /tasks`、`GET /tasks/{id}`、`GET /tasks` | ✅ | `api/app.py` | 4 个 TestClient 测试 | 后台线程异步 + 轮询，工厂可注入 |
| 工具重试 | ErrorType 分类 + RetryPolicy 纯决策 + Executor 集成 | ✅ | `retry/retry_policy.py` | 12 个单测 + retry demo | 只救瞬态，不掩盖确定性错误 |
| 重试观测 | `TaskStep.attempts` + `task.metrics["retry_events"]`（8 字段） | ✅ | `agent/executor.py` | retry 单测断言字段完整性 | 供 Phase 2 Evaluation 直接统计 |
| 端到端 Demo | CSV→OSS→pre-hook→沙箱→Agent→save_report→URL | ✅ | `demo/run_demo.py` | 真机运行（需 docker + LLM key） | 垂直链路真实可演示 |
| 单元测试 | 13 文件 / 79 用例，全离线 | ✅ | `tests/` | `pytest -q`：79 passed | fake LLM/SDK/S3，无外部依赖 |
| 设计文档 | 8 篇，统一 10 小节模板 | ✅ | `docs/design/` | 人工评审 | 先评审后实现（07 评审 → 08 实现） |
| 二进制持久化 | docker-compose 卷持久化 + API key + host-gateway | ✅ | `docker-compose.yaml` | 本地 docker 验证 | 与官方推荐的 pin 镜像对齐 |
| 权限/安全层 | 工具/文件/对象前缀权限，独立会话与超时 | IN_PROGRESS | `security/`、Registry、SandboxClient | 离线已验证，真机待验证 | 任意 Python 仍以容器为边界 |
| 评估体系 | 成功率/耗时/Token 统计 | ⬜ Phase 2 | `evaluation/`（未建，`metrics` 已预留） | — | 字段已预留，接上即可 |
| Memory | 短期（任务上下文）+ 长期（历史任务） | ⬜ Phase 2 | `memory/`（未建） | — | 计划 Redis |
| MCP 接入 | markitdown（文档解析核心能力） | ⬜ Phase 2 | `tools/mcp_adapter.py`（未建） | — | 文档 Agent 的核心能力来源 |
| 持久化队列 | Redis/Celery 替换 threading | ⬜ Phase 2 | 未实现 | — | 进程重启丢 in-flight 任务 |
| Token 统计 | `response.usage` 累加 | ⬜ 未做 | `agent/executor.py` | — | `Task.metrics` 预留 |
| CI/CD | 流水线 / coverage / lint | ⬜ 未做 | — | — | 当前手工跑 pytest |

---

## 2. 模块级能力明细

### 2.1 task/ —— 任务生命周期（410 行）

| 能力 | 实现 | 位置 |
| --- | --- | --- |
| 任务状态机 | `CREATED → RUNNING → SUCCESS/FAILED`，`_ALLOWED_TRANSITIONS` 集中校验 | `task/task_model.py` |
| 步骤状态机 | `PENDING → RUNNING → SUCCESS/FAILED`，含 `duration_ms` 自动计算 | `task/task_model.py` |
| 序列化契约 | `to_dict/from_dict`，跨存储/跨进程的前提 | `task/task_model.py` |
| 生命周期门面 | create/start/succeed/fail + 步骤管理 + 指标事件 | `task/task_manager.py` |
| 存储协议 | `TaskStore` ABC + 线程安全内存实现 | `task/task_manager.py` |

### 2.2 sandbox/ —— 沙箱集成层（224 行）

| 能力 | 实现 | 位置 |
| --- | --- | --- |
| 防腐层 | `SandboxClient` 归一化 SDK，上层不依赖 SDK 细节 | `sandbox/client.py` |
| 二进制安全 | base64 写 + `download_file` 字节流读 | `sandbox/client.py` |
| 输出归一化 | `ExecutionResult`：status/stdout/stderr/error/traceback/outputs | `sandbox/client.py` |
| 鉴权 | `X-AIO-API-Key` header 注入（SDK 无构造参数） | `sandbox/client.py` |
| 数据搬运 | `HermesHookEngine` pre/post 闭环 + 沙箱清洗 | `sandbox/hooks.py` |

### 2.3 agent/ —— 编排层（407 行）

| 能力 | 实现 | 位置 |
| --- | --- | --- |
| 规划 | `create_plan` 函数调用 + `tool_choice` 强制 | `agent/planner.py` |
| 多轮执行 | 工具调用循环 + 消息历史回传 + 重试内层循环 | `agent/executor.py` |
| 生命周期编排 | 创建→启动→规划→执行→成功/失败 | `agent/orchestrator.py` |
| LLM 适配 | 薄封装，可注入 fake | `agent/llm_client.py` |
| 组合根 | 一处装配全部依赖 | `agent/wiring.py` |

### 2.4 tools/ —— 工具层（230 行）

| 工具 | 职责 | Schema 关键参数 |
| --- | --- | --- |
| `run_python` | 沙箱内执行 Python，返回归一化输出 | code / timeout |
| `read_file` | 读沙箱 workspace 内文本文件 | filename |
| `save_report` | 沙箱产物 → OSS → presigned URL | sandbox_filename / oss_key |

### 2.5 retry/ —— 重试层（95 行）

| 能力 | 实现 |
| --- | --- |
| 错误分类 | `ErrorType` 6 类，`retryable` 属性（TRANSIENT/TIMEOUT 可重试） |
| 纯决策 | `RetryPolicy.decide(attempt, error_type) → RetryDecision`（无 IO，可穷举单测） |
| 退避 | 指数退避 + 抖动（±50%），`max_delay` 封顶 |
| 兜底分类 | `classify_exception` 处理工具抛异常的场景 |
| 集成 | `_invoke_tool` 内层重试循环，与外层 tool-calling 正交 |

### 2.6 api/ —— API 层（66 行）

| 端点 | 语义 |
| --- | --- |
| `POST /tasks` | 创建任务立即返回 id，后台线程异步执行 |
| `GET /tasks/{id}` | 轮询状态/步骤/结果；404 处理 |
| `GET /tasks` | 列出全部任务 |

### 2.7 storage/ + core/ —— 基础设施（146 行）

- `StorageManager`：懒创建 bucket、put/get 字节、presigned URL、`StorageError` 包装。
- `Settings`：pydantic-settings 映射 `.env`/环境变量；`get_settings()` 单例、import 无副作用。

---

## 3. 工程能力自评

| 维度 | 现状 | 说明 |
| --- | --- | --- |
| 可测性 | ★★★★★ | 79 个用例全部离线（fake LLM/SDK/S3），6 秒内跑完 |
| 可扩展性 | ★★★★☆ | 工具/存储/LLM 均接口化；缺 Memory/安全/MCP |
| 可观测性 | ★★★★☆ | 任务+步骤全轨迹、重试 8 字段事件；缺 metrics 落库 |
| 健壮性 | ★★★★☆ | 状态机+锁、重试、错误包装；缺权限层和持久化队列 |
| 文档 | ★★★★★ | README + 8 篇 10 小节设计笔记，含面试 Q&A |
| 生产就绪 | ★★☆☆☆ | MVP 定位明确；Phase 2 未完成项见下 |

---

## 4. Phase 2 待补齐清单（演进路线）

1. `security/`：工具/文件/危险命令权限控制
2. `evaluation/`：成功率、工具调用次数、耗时、Token 消耗
3. `memory/`：短期（任务上下文）+ 长期（历史任务），Redis
4. `tools/mcp_adapter.py`：接入沙箱预置 MCP server（markitdown / file / shell）
5. Redis `TaskStore` + Celery/队列替换后台线程
6. LLM 调用重试（复用 RetryPolicy）
7. 工具结果截断/摘要回传
8. 并行工具调用
9. `SandboxError`/`StorageError` 自带 `error_type`，完善分类
10. CI + coverage + lint

---

## 5. 验证与使用速查

```powershell
# 全量测试（离线）
python -m pytest -q          # 79 passed

# 端到端 Demo（需 docker compose up -d + .env 的 LLM_API_KEY）
python -m demo.run_demo

# 重试场景验证（脚本化 LLM，无需外部依赖）
python -X utf8 -m demo.retry_demo

# 启动 API
python -m uvicorn api.app:app --host 127.0.0.1 --port 8000
```
