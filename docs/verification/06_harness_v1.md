# P0-5 Harness V1 回归与真实环境验证记录

> 记录日期：2026-09-18  
> 实现分支：`feat/harness-p0-5`  
> 开发基线：`feat/harness-p0-4` / `195ba1d`  
> 当前状态：离线回归已于 2026-09-28 执行并通过（见 4.2 节）；真实 Docker 烟测同日已
> 执行但**未通过**——`security_smoke` 退出码 1，`timeout` 探针暴露"超时不是硬上限"
> （见 5.3 节），P0-5 尚未收口。

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
| Docker Engine / Compose | 29.7.2 / v5.4.0（Docker Desktop 4.87.0），第 5 节已执行 |
| `agent-sandbox` SDK | `0.0.30`（`requirements.txt` 锁定） |
| 沙箱镜像 tag / image ID | `...all-in-one-sandbox:1.11.0` / `sha256:6328d7fd2f0ff0b4c147c3d05b3df1ce331f4a482eb6e550ecd64ed1fcf906e7` |

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

- `security_smoke`：**FAILED**（退出码 `1`，中止在 timeout 探针断言）
- timeout probe：**不通过**：请求的 `timeout` 不是执行硬上限，延迟副作用仍会落地
- 退出码：`1`

实测环境：

| 项目 | 值 |
| --- | --- |
| Docker Client / Server | 29.7.2 / 29.7.2（Docker Desktop 4.87.0，Engine API 1.55） |
| Docker Compose | v5.4.0 |
| 沙箱镜像 | `enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0` |
| 镜像 image ID | `sha256:6328d7fd2f0ff0b4c147c3d05b3df1ce331f4a482eb6e550ecd64ed1fcf906e7` |
| 容器实测配额 | Memory `4294967296`；MemorySwap == Memory；PidsLimit `512`；NanoCpus `2000000000`（2 CPU） |
| 端口绑定 | `127.0.0.1:8080 -> 8080/tcp`（仅本机） |
| 容器健康 | `healthy` |
| `agent-sandbox` SDK | `0.0.30` |

`.venv\Scripts\python.exe -m demo.security_smoke` 原始输出：

```text
PASS running container quotas: {'Memory': 4294967296, 'NanoCpus': 2000000000, 'PidsLimit': 512, 'MemorySwap': 4294967296}
PASS real file write/read
PASS traversal denied
PASS independent Jupyter sessions
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  ...
  File "D:\gitcode\document-specialist-agent\demo\security_smoke.py", line 54, in main
    assert response.status == "timeout" and response.execution_uncertain, response.text
AssertionError: [error] Code execution error
exit_code=1
```

该断言与镜像实际行为不符，属于测量工具本身的缺陷，已先行修复（见 5.3.3）。修复后
重跑，失败原因变为真实问题：

```text
PASS running container quotas: {'Memory': 4294967296, 'NanoCpus': 2000000000, 'PidsLimit': 512, 'MemorySwap': 4294967296}
PASS real file write/read
PASS traversal denied
PASS independent Jupyter sessions
PASS sandbox reported a non-ok status (observed='error')
PASS sandbox envelope reported success=false (observed=False)
PASS client marked the result execution_uncertain (observed=True)
PASS Harness maps it to a failed ToolResult (observed=False)
PASS Harness marks it terminal, so it is never replayed (observed=True)
     status='error' error='Code execution error' elapsed=3.49s
FAIL delayed side effect exists: the timed-out execution kept running past the requested timeout (1s) and wrote /home/gem/workspace/security-probe-...-late.txt
FAIL real smoke checks: delayed side effect after timeout
exit_code=1
```

#### 5.3.1 根因诊断

用一次性探针固定请求的 `timeout`、改变被测代码的 `sleep` 时长，记录调用耗时、返回状态
与"延迟副作用文件"是否出现（探针为临时文件，取证后已删除）：

| 代码 sleep | 请求 timeout | 实际耗时 | 返回 status | 延迟副作用文件 |
| --- | --- | --- | --- | --- |
| 3s | 1s | 3.49s | `error`（`execution_uncertain=True`） | **存在** |
| 2s | 1s | 2.56s | `error`（`execution_uncertain=True`） | **存在** |
| 6s | 1s | 6.62s | `error`（`execution_uncertain=True`） | **存在** |
| 12s | 1s | 6.60s | `error`（`execution_uncertain=True`） | 不存在 |
| 12s | 8s | 12.53s | `error`（`execution_uncertain=True`） | **存在** |

原始响应体为 `ResponseJupyterExecuteResponse(success=False, message="Code execution error")`，
`data.status="error"`，`outputs[0]` 为 `output_type="error"`、`ename="KernelError"`、
`evalue=""`。

结论：

1. `timeout` **不是**执行硬上限。请求 1 秒时，2 秒与 6 秒的代码仍完整跑到结束并产生
   副作用；请求 8 秒时，12 秒的代码同样跑完。有效终止点约在"请求值 + 数秒"处，与请求
   值并不同步，也不受调用方精确控制。
2. 因此 5.2 节"timeout 探针等待后延迟副作用文件不存在"**不成立**：副作用是否发生取决于
   它落在有效终止点之前还是之后。
3. 该镜像返回 `success=false` + `KernelError`，`SandboxClient._normalize` 走
   "success 为 False"分支，映射为 `status="error"`、`execution_uncertain=True`。
   原 `demo/security_smoke.py` 里 `status == "timeout"` 的断言在 1.11.0 上**永远不成立**，
   脚本会在真正的安全断言之前中止——测量工具缺陷，已修复（见 5.3.3）。

#### 5.3.3 烟测脚本修订（已完成）

修订前的脚本用 `assert response.status == "timeout"` 判定超时，会被状态映射差异提前
截断，导致下面真正的"延迟副作用仍发生"无法被稳定检测。修订后的判定逻辑：

1. 不再要求 `status == "timeout"`，把 `error` 与 `timeout` 都视为非成功结果；
2. 核心契约断言改为：非 `ok` 状态、原始信封 `success is False`、
   `execution_uncertain is True`、`execution_to_tool_result` 返回失败且 `terminal is True`
   （即 Harness 判为终止性错误、禁止重放）；
3. **无论状态如何映射**，都继续等待到"代码延迟 + 余量"之后再检查延迟副作用文件；
4. 只要延迟副作用文件存在，脚本仍然失败并给出明确原因。

探针参数同时从 `sleep 6 / timeout 1` 改为 `sleep 3 / timeout 1`：原来的组合恰好落在服务端
有效终止点附近，属于边界竞态；3 秒的副作用稳定落在终止点之前，失败可复现而非偶然。

> 这一步是校准测量工具，**不代表接受当前风险**。修复后脚本依然失败，且失败原因正是
> 真正的安全问题；P0-5 继续保持未完成。

#### 5.3.2 5.2 节逐项判定

| 5.2 节要求 | 结果 |
| --- | --- |
| CPU、内存、swap、PID 配额与 compose 配置一致 | 通过 |
| 宿主端口绑定符合本地安全配置 | 通过（仅 `127.0.0.1:8080`） |
| 文件写入/读取、越界路径拒绝、独立执行会话通过 | 通过 |
| timeout 返回明确的超时或不确定状态 | 部分通过：wrapper 标记 `execution_uncertain=True`，但 `status` 为 `error` 而非 `timeout` |
| timeout 探针等待后，延迟副作用文件不存在 | **不通过**（见 5.3.1） |
| 脚本退出码为 0 | **不通过**（退出码 1；修订后失败原因为"延迟副作用仍存在"，不再是状态断言） |

> 分层结论（重要）：`tools/sandbox_tool.py` 使用
> `terminal = execution_uncertain or status == "timeout"` 判定，因此在上述场景中工具错误
> **仍然是终止性错误**，任务不会重放这段代码——Harness 层的"不确定即终止、禁止重放"
> 成立。不成立的是更下面一层：沙箱没有在请求的超时点真正停下代码，副作用仍可能落地。

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

本次（2026-09-28）达成情况：条件 1、2、4、5 满足；**条件 3 不满足**。Docker 已可用，
5.1 节命令全部执行，`security_smoke` 退出码为 1，`timeout` 探针未通过（见 5.3）。
因此本页结论是"八条离线端到端场景通过、真实沙箱超时语义不满足 5.2 节要求"，
`task_points.md` 中 P0-5 仍不能标记为已完成。

剩余工作分两步，且**不能**用第一步代替第二步：

1. ~~修订 `demo/security_smoke.py`，使其断言与镜像实际返回的
   `success=false` + `execution_uncertain=True` 一致。~~ 已完成（见 5.3.3），
   脚本现在准确报告失败原因；
2. 作为独立安全任务处理真正的语义问题：把"沙箱在请求超时点停下代码"变成硬性保证，
   而不是依赖 `execute_code(timeout=...)` 的服务端语义。修复前 P0-5 不得收口，
   也不得把本页任何一项标记为完全通过。已完成的 `hard_timeout` 能力探针证明 shell
   路径同样不能终止执行（8/8 泄漏），证据见
   [`10_hard_timeout_probe.md`](10_hard_timeout_probe.md)。
