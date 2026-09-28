# P0-5 Harness V1 回归与真实环境验证记录

> 记录日期：2026-09-18  
> 实现分支：`feat/harness-p0-5`  
> 开发基线：`feat/harness-p0-4` / `195ba1d`  
> 当前状态：离线回归已于 2026-09-28 执行并通过（见 4.2 节）；真实 Docker 烟测因验收机
> 未安装 Docker 而**未执行**（见 5.3 节），P0-5 尚未收口。

本文档是一次验收记录，不是对
[`01_security.md`](01_security.md) 的覆盖或改写。`01_security.md` 保留当时的历史结论；
本页只记录 Harness V1 当前版本的验证结果。

## 1. 验收对象

| 项目 | 验收结果 |
| --- | --- |
| Commit SHA | `b9bbe72c57831572364c2ac8bfa7a62229514189` |
| 工作区状态 | 执行测试时为空；随后仅新增本验收记录文件 |
| 操作系统 | Windows 11 25H2（build 26200） |
| Python / pytest | 3.13.2（工作区 `.venv`）/ 9.1.1 |
| Docker Engine / Compose | **不可用**：验收机未安装 `docker` 命令，第 5 节未执行 |
| `agent-sandbox` SDK | `0.0.30`（`requirements.txt` 锁定） |
| 沙箱镜像 tag / image ID | 未验证（Docker 不可用） |

验收者必须在执行命令前填写准确 SHA，并在完成后附上 `git status --short`。
禁止将未执行的项目填写为 PASS。

## 2. 八条端到端 Fake LLM 场景

统一入口为 `AgentOrchestrator.run`，并实际经过 Planner、Executor、Tool Registry
与 TaskManager。Fake LLM 只替代外部模型响应，不绕过 Runtime。

| 场景 | 测试 | 核心断言 |
| --- | --- | --- |
| 静态计划正常完成 | `test_static_plan_completes_through_the_full_harness_path` | Tool Call 绑定计划步骤、显式完成、任务成功 |
| 工具失败后换路 | `test_tool_failure_is_observed_then_the_model_changes_route` | 首工具失败回注、备用工具成功、不误触发重规划 |
| 数据缺失局部重规划 | `test_missing_data_requests_one_local_replan_then_finishes_the_new_step` | 原因码、计划 v1→v2、新步骤完成 |
| 大结果卸载与分页回读 | `test_large_result_is_offloaded_and_recalled_one_bounded_page` | 上下文无完整结果、TaskStep 保存 result_ref、按页回读 |
| 上下文压缩 | `test_soft_limit_compacts_complete_history_before_the_next_model_call` | soft limit 触发、结构化摘要进入下一轮 |
| 压缩熔断 | `test_repeated_compaction_failure_opens_the_circuit_and_uses_safe_trim` | 连续失败后停止调用压缩模型，改用确定性整组裁剪 |
| 最大执行轮数 | `test_maximum_execution_rounds_fail_the_task_instead_of_looping_forever` | 达到预算后抛明确错误并把任务收口为 FAILED |
| 最大重规划次数 | `test_maximum_replans_fail_the_task_instead_of_replanning_forever` | 只应用一次重规划，预算耗尽后 FAILED |

测试文件：[`tests/test_harness_v1_scenarios.py`](../../tests/test_harness_v1_scenarios.py)。

## 3. 简历描述追踪矩阵

| 简历关键词 | 主要实现位置 | 直接证据 |
| --- | --- | --- |
| O-P-E 三层架构 | `agent/orchestrator.py`、`agent/planner.py`、`agent/executor.py` | 静态计划端到端场景 |
| ReAct / Tool Calling Loop | `agent/executor.py:Executor.run` | 静态计划、失败换路、最大执行轮数场景 |
| 结构化动态规划 | `task/plan_model.py`、`task/plan_state.py` | Planner/计划模型测试 + 局部重规划场景 |
| Tool Call 与计划步骤绑定 | `agent/executor.py:_run_tool_call`、`task/task_model.py:TaskStep` | 静态计划场景 + `tests/test_executor_plan_binding.py` |
| 局部重规划 | `agent/executor.py:_replan`、`agent/planner.py:replan` | 数据缺失重规划、最大重规划次数场景 |
| 可插拔工具注册与分发 | `tools/tool_registry.py`、`tools/base_tool.py` | 静态计划与失败换路场景 + Registry 单元测试 |
| Hook 大结果卸载 | `context/hooks.py`、`context/tool_output_store.py` | 大结果卸载与回读场景 |
| result_ref 二次回读 | `tools/tool_output_tool.py` | 大结果卸载与回读场景 |
| 上下文预算与完整组压缩 | `context/manager.py`、`context/message_groups.py`、`context/compactor.py` | 上下文压缩场景 |
| 压缩有限重试与熔断 | `context/compactor.py`、`context/manager.py` | 压缩熔断场景 |
| 任务/步骤状态与执行轨迹 | `task/task_model.py`、`task/task_manager.py`、`task/file_task_store.py` | 八条场景的 Task/TaskStep/metrics 断言 |
| 错误分类与有限重试 | `tools/base_tool.py:ErrorType`、`retry/retry_policy.py` | `tests/test_retry.py`、`tests/test_retry_integration.py` |
| 沙箱隔离与安全终止 | `sandbox/sandbox_client.py`、`security/`、`demo/security_smoke.py` | 离线安全测试 + 本页真实 Docker 烟测 |

这张表只说明“证据在哪里”。最终能否无保留用于简历，仍以第 4、5 节的实际结果为准。

## 4. 离线回归

### 4.1 建议执行顺序

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_harness_v1_scenarios.py
.venv\Scripts\python.exe -m pytest -q
```

Linux / macOS：

```bash
.venv/bin/python -m pytest -q tests/test_harness_v1_scenarios.py
.venv/bin/python -m pytest -q
```

### 4.2 结果

- Harness V1 聚焦测试：`8 passed in 1.63s`
- 完整 pytest：`364 passed, 1 skipped, 1 warning in 11.95s`
- 新增/修改测试数量：新增 12 个测试文件（70 个测试函数）；修改 7 个既有测试文件
- 已知环境跳过：1 项，`tests/test_security.py:102`（Windows 无法创建符号链接）
- 失败堆栈：无

完整输出：

```text
........................................................................ [ 19%]
........................................................................ [ 39%]
........................................................................ [ 59%]
......................................................s................. [ 78%]
........................................................................ [ 98%]
.....                                                                    [100%]
=========================== short test summary info ===========================
SKIPPED [1] tests\test_security.py:102: OS/user cannot create symlinks (on Windows: enable Developer Mode)
364 passed, 1 skipped, 1 warning in 11.95s
```

聚焦场景输出：

```text
........                                                                 [100%]
8 passed in 1.63s
```

新增的 12 个测试文件为 `test_harness_v1_scenarios.py`、`test_executor_plan_binding.py`、
`test_plan_state.py`、`test_memory_{store,policy,extractor,service,prompting,api}.py`、
`test_orchestrator_memory.py`、`test_metering.py`、`test_evaluation.py`。

> 验收起始状态必须记录：验收前 `main`（`4899a5c`）直接执行 `python -m pytest -q` 会在
> **收集阶段**失败，原因是 `memory/extractor.py` 顶层的 `agent.llm_client` 导入构成
> `agent.llm_client → retry → tools → memory.extractor` 环；另外以 `context` 为首个导入
> 的入口（例如 P1-3 文档推荐的 `tests/test_metering.py`）会触发 `context/compactor.py`
> 的同类环。两处均在本次验收中修复（`470cb10`、`b9bbe72`）后，上表结果才成立。

## 5. 真实 Docker 安全烟测

### 5.1 前置检查与命令

```powershell
docker version
docker compose version
docker compose up -d
docker compose ps
docker inspect doc-agent-sandbox
.venv\Scripts\python.exe -m demo.security_smoke
```

不要使用 `docker compose down -v`，避免删除用户已有卷。若容器名与本地配置不同，
先通过 `docker compose ps` 确认精确目标。

### 5.2 必须确认

- CPU、内存、swap、PID 配额与 compose 配置一致；
- 宿主端口绑定符合本地安全配置；
- 文件写入/读取、越界路径拒绝、独立执行会话通过；
- timeout 返回明确的超时或不确定状态；
- timeout 探针等待后，延迟副作用文件不存在；
- 脚本退出码为 0。

### 5.3 结果

- `security_smoke`：`NOT RUN`
- timeout probe：`NOT RUN`
- 退出码：`N/A`

完整输出：

```text
docker: The term 'docker' is not recognized as a name of a cmdlet, function, script file,
or executable program. Check the spelling of the name, or if a path was included, verify
that the path is correct and try again.
```

验收机未安装 Docker Engine / Compose，5.1 节的前置检查无法通过。按本页"禁止将未执行
的项目填写为 PASS"的要求，第 5 节整体记录为 `NOT RUN`，既不记为通过，也不记为失败。

## 6. 必须保留的安全边界

即使 `security_smoke` 与延迟写文件探针通过，也只能证明当前镜像、当前配置和该探针
路径下的行为。它**不能证明任意恶意代码派生出的所有子进程都一定被终止**。

当前项目仍是本地 Harness 原型：沙箱降低模型生成代码直接影响宿主机的风险，但不应
表述为已经获得形式化证明的强隔离。生产级强隔离仍需要更严格的容器生命周期、内核与
网络策略，以及针对逃逸、派生进程和资源耗尽的专门测试。

## 7. P0-5 收口规则

只有在以下条件全部满足后，才把 `task_points.md` 中 P0-5 改为“已完成”：

1. 八条 Harness V1 聚焦场景全部通过；
2. 完整 pytest 回归通过，或每个跳过/失败都有可复现的环境说明；
3. 真实 Docker `security_smoke` 退出码为 0，timeout probe 通过；
4. 本页填入准确环境、SHA 与完整输出；
5. 验收后工作区无意外改动。

本次（2026-09-28）达成情况：条件 1、2、4、5 满足；**条件 3 不满足**，因为验收机没有
Docker，真实沙箱烟测未执行。因此本页结论是"八条离线端到端场景通过、真实 Docker
验证缺口仍在"，`task_points.md` 中 P0-5 尚不能标记为已完成。
