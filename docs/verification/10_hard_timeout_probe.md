# Hard Timeout 能力探针：shell `hard_timeout` 是否真正终止执行

> 记录日期：2026-09-28
> 实现分支：`main`
> 探针脚本：[`demo/hard_timeout_probe.py`](../../demo/hard_timeout_probe.py)（提交 `b70ccc0`）
> 当前状态：**能力探针已完成，8/8 场景泄漏；未接入生产代码**

本页只回答一个问题：AIO Sandbox 的 shell 接口是否可以作为
`SandboxClient.execute_python` 的硬超时基础。结论是否定的，证据如下。本页不修改
生产执行链路，`SandboxClient.execute_python` 保持现状。

---

## 1. 动机

[`06_harness_v1.md`](06_harness_v1.md) 第 5.3 节记录：`security_smoke` 的 timeout 探针
证明 `jupyter.execute_code(timeout=...)` 不影响执行，延迟副作用仍会落地。SDK 文档称
shell 接口的 `hard_timeout` 与 `timeout` 不同：

> Hard timeout (seconds) for command execution. When reached, the command is forcefully
> stopped and current console output is returned with HARD_TIMEOUT status. Unlike `timeout`
> (which only affects HTTP response timing), this actually terminates the command.
> —— `agent_sandbox/shell/client.py`

因此需要实测：`hard_timeout` 是否真的能终止命令。若可以，才有资格评估把
`execute_python` 迁移为"临时文件 + shell + `hard_timeout`"。

---

## 2. 验收对象

| 项目 | 值 |
| --- | --- |
| 分支 / 探针提交 | `main` / `b70ccc0` |
| Docker Client / Server | 29.7.2 / 29.7.2（Docker Desktop 4.87.0） |
| Docker Compose | v5.4.0 |
| 沙箱镜像 | `enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0` |
| 镜像 image ID | `sha256:6328d7fd2f0ff0b4c147c3d05b3df1ce331f4a482eb6e550ecd64ed1fcf906e7` |
| `agent-sandbox` SDK | `0.0.30` |
| 沙箱地址 / 工作区 | `http://localhost:8080` / `/home/gem/workspace` |
| 执行入口 | `client.shell.exec_command(command=..., hard_timeout=..., timeout=hard_timeout+20)` |
| 探针参数 | `PROBE_MARGIN_SECONDS = 3.0` |

复现命令（需要 Docker 与 Compose 已启动）：

```powershell
.venv\Scripts\python.exe -m demo.hard_timeout_probe
```

---

## 3. 场景设计

探针把"**API 是否返回**"和"**进程是否还活着**"分开测量，这是本次最关键的设计：

1. 每次 `exec_command` 返回后**立刻**扫描 `/proc` 中带唯一标签的进程，得到
   `immediately_after_timeout`；
2. 再等到"代码延迟 + 余量"之后，检查延迟副作用文件与残留进程。

进程识别使用唯一标签（`htprobe-<场景>-<8位随机>`），扫描脚本排除自身命令行。覆盖场景：

| # | 场景 | hard_timeout | 类型 |
| --- | --- | --- | --- |
| 1 | 当前进程 sleep 2s | 1s | required |
| 2 | 当前进程 sleep 6s | 1s | required |
| 3 | 当前进程 sleep 12s | 1s | required |
| 4 | 当前进程 sleep 12s | 8s | required |
| 5 | 普通子进程（父进程等待子进程） | 1s | required |
| 6 | `start_new_session=True` 脱离进程组 | 1s | boundary |
| 7 | shell `&` 后台任务 | 1s | boundary |
| 8 | 当前进程 + 写文件后继续存活 30s | 1s | required |

---

## 4. 原始输出

```text
sandbox base_url : http://localhost:8080
workspace        : /home/gem/workspace
probe root       : /home/gem/workspace/hard-timeout-probe-f67ac6fa
scenarios        : 8

--- current-process sleep=2s hard_timeout=1s [required] ---
  expectation : the running command must stop before its delayed write
  returned    : success=True status='hard_timeout' exit_code=-1 elapsed=1.18s overhead=+0.18s
  message     : 'Command executed'
  output      : ''
  processes   : immediately_after_timeout=1 after_wait=0 session=cc0e26c0-fc4f-4e12-8f5f-5d66d1ec26f5
  after wait  : marker=present
  isolation   : {'status': 'completed', 'exit_code': 0, 'output': 'isolation-ok'}
  verdict     : LEAKED

--- current-process sleep=6s hard_timeout=1s [required] ---
  expectation : the running command must stop before its delayed write
  returned    : success=True status='hard_timeout' exit_code=-1 elapsed=1.20s overhead=+0.20s
  message     : 'Command executed'
  output      : ''
  processes   : immediately_after_timeout=1 after_wait=0 session=1aac484d-2325-4914-a344-20cb4827d047
  after wait  : marker=present
  isolation   : {'status': 'completed', 'exit_code': 0, 'output': 'isolation-ok'}
  verdict     : LEAKED

--- current-process sleep=12s hard_timeout=1s [required] ---
  expectation : the running command must stop before its delayed write
  returned    : success=True status='hard_timeout' exit_code=-1 elapsed=1.25s overhead=+0.25s
  message     : 'Command executed'
  output      : ''
  processes   : immediately_after_timeout=1 after_wait=0 session=8ac890ec-a57d-4df9-b4fe-7c7750bfdcf7
  after wait  : marker=present
  isolation   : {'status': 'completed', 'exit_code': 0, 'output': 'isolation-ok'}
  verdict     : LEAKED

--- current-process sleep=12s hard_timeout=8s [required] ---
  expectation : the running command must stop before its delayed write
  returned    : success=True status='hard_timeout' exit_code=-1 elapsed=8.26s overhead=+0.26s
  message     : 'Command executed'
  output      : ''
  processes   : immediately_after_timeout=1 after_wait=0 session=fd8fd59c-898d-493b-a866-b65bdb99a470
  after wait  : marker=present
  isolation   : {'status': 'completed', 'exit_code': 0, 'output': 'isolation-ok'}
  verdict     : LEAKED

--- ordinary-child sleep=3s hard_timeout=1s [required] ---
  expectation : child shares the process group; it must not outlive the kill
  returned    : success=True status='hard_timeout' exit_code=-1 elapsed=1.25s overhead=+0.25s
  message     : 'Command executed'
  output      : ''
  processes   : immediately_after_timeout=2 after_wait=0 session=a0ae36cd-3aeb-4948-8a02-2c1ad98077e5
  after wait  : marker=present
  isolation   : {'status': 'completed', 'exit_code': 0, 'output': 'isolation-ok'}
  verdict     : LEAKED

--- detached-child start_new_session=True sleep=3s hard_timeout=1s [boundary] ---
  expectation : new session; likely escapes a process-group kill
  returned    : success=True status='hard_timeout' exit_code=-1 elapsed=1.22s overhead=+0.22s
  message     : 'Command executed'
  output      : ''
  processes   : immediately_after_timeout=2 after_wait=2 session=1a7e1bfa-511a-4c1b-b97e-c009576e8166
  after wait  : marker=present
  verdict     : LEAKED

--- shell-background sleep=3s hard_timeout=1s [boundary] ---
  expectation : command returns immediately, so hard_timeout never fires
  returned    : success=True status='completed' exit_code=0 elapsed=0.33s overhead=-0.67s
  message     : 'Command executed'
  output      : '[1] 8027\nbackground-started'
  processes   : immediately_after_timeout=1 after_wait=1 session=9d556fef-577b-438b-a303-623ce96a798e
  after wait  : marker=present
  verdict     : LEAKED

--- current-process-lingering sleep=3s linger=30s hard_timeout=1s [required] ---
  expectation : if the process is alive right after the timeout, nothing was killed
  returned    : success=True status='hard_timeout' exit_code=-1 elapsed=1.26s overhead=+0.26s
  message     : 'Command executed'
  output      : ''
  processes   : immediately_after_timeout=1 after_wait=1 session=890de8a4-df0b-4b12-b3bf-9d43313b4ca0
  after wait  : marker=present
  isolation   : {'status': 'completed', 'exit_code': 0, 'output': 'isolation-ok'}
  verdict     : LEAKED

=== summary ===
required contained : 0/6
boundary contained : 0/2
overhead over hard_timeout (s): min=-0.67 max=+0.26
REQUIRED LEAK      : current-process sleep=2s hard_timeout=1s
REQUIRED LEAK      : current-process sleep=6s hard_timeout=1s
REQUIRED LEAK      : current-process sleep=12s hard_timeout=1s
REQUIRED LEAK      : current-process sleep=12s hard_timeout=8s
REQUIRED LEAK      : ordinary-child sleep=3s hard_timeout=1s
REQUIRED LEAK      : current-process-lingering sleep=3s linger=30s hard_timeout=1s
boundary leak      : detached-child start_new_session=True sleep=3s hard_timeout=1s
boundary leak      : shell-background sleep=3s hard_timeout=1s

=== capability boundary ===
hard_timeout did NOT contain every required scenario.
Conclusion: do not switch execute_python onto shell+hard_timeout yet.
exit_code=1
```

---

## 5. 结果汇总

| 场景 | status | 实际耗时 | overhead | 超时后立即存活进程 | 等待后残留 | 延迟副作用文件 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 当前进程 sleep 2s / ht 1s | `hard_timeout` | 1.18s | +0.18s | 1 | 0 | **存在** | LEAKED |
| 当前进程 sleep 6s / ht 1s | `hard_timeout` | 1.20s | +0.20s | 1 | 0 | **存在** | LEAKED |
| 当前进程 sleep 12s / ht 1s | `hard_timeout` | 1.25s | +0.25s | 1 | 0 | **存在** | LEAKED |
| 当前进程 sleep 12s / ht 8s | `hard_timeout` | 8.26s | +0.26s | 1 | 0 | **存在** | LEAKED |
| 普通子进程 sleep 3s / ht 1s | `hard_timeout` | 1.25s | +0.25s | 2 | 0 | **存在** | LEAKED |
| 脱离进程组 `start_new_session=True` | `hard_timeout` | 1.22s | +0.22s | 2 | 2 | **存在** | LEAKED |
| shell `&` 后台任务 | `completed` | 0.33s | -0.67s | 1 | 1 | **存在** | LEAKED |
| 当前进程 + linger 30s | `hard_timeout` | 1.26s | +0.26s | 1 | 1 | **存在** | LEAKED |

**required 场景 0/6 通过，boundary 场景 0/2 通过，整体 8/8 泄漏。**

---

## 6. 返回语义

`hard_timeout` 触发时的原始字段：

```text
success   = True
status    = 'hard_timeout'
exit_code = -1
message   = 'Command executed'
output    = ''
```

对 Harness 映射有三个直接后果：

1. **`success` 为 `True`**，即使 `status` 是 `hard_timeout`。任何以 `success` 判断成败的
   映射都会把硬超时当成成功，这是必须显式规避的陷阱。
2. **`output` 为空**。按 `ShellCommandResult` 的定义，`output` 只在 `status == completed`
   时有值；超时场景的片段输出需要读取 `console` 记录（或另调用 `view`）。
3. **两条执行路径的失败表示完全不同**：Jupyter 路径是 `success=false` + `KernelError`
   （`SandboxClient._normalize` 映射为 `status="error"`、`execution_uncertain=True`），
   shell 路径是 `success=true` + `status="hard_timeout"` + `exit_code=-1`。两者不能共用
   一套映射逻辑。

---

## 7. 时间边界与会话隔离

- **时间边界**：`hard_timeout` 返回时机非常精确，8 个场景的 overhead 落在
  `-0.67s ~ +0.26s`，前四组对照分别为 +0.18 / +0.20 / +0.25 / +0.26 秒。
  返回准时**恰恰是危险之处**：接口看起来守时，实际没有停止任何东西。
- **会话隔离**：8 个场景在超时后于同一 `session_id` 继续执行 `echo isolation-ok`，
  全部返回 `completed / exit_code=0 / isolation-ok`。硬超时不会污染会话。

---

## 8. 探针自身的残留检查

探针在 `finally` 中按唯一标签 SIGKILL 残留进程、删除标记文件、清理会话。运行后在容器内
验证：

```text
$ docker exec doc-agent-sandbox sh -c "ls -a /home/gem/workspace; ps -ef | grep htprobe | grep -v grep | wc -l"
.
..
Downloads
high_performers.csv
input.csv
probe.txt
report.pdf
reports
reports_persist_check.csv
sales.xlsx
tasks
--- tagged processes ---
0
```

`htprobe-` 残留进程为 0，`hard-timeout-probe-*` 目录已删除，工作区只剩原有文件。

---

## 9. 能力边界结论

1. **SDK 文档与本镜像实际行为不一致。** 文档称 `hard_timeout` 会 "forcefully stop" 命令；
   实测是：API 在 `hard_timeout` 时刻准时返回 `HARD_TIMEOUT` 状态，但**进程继续运行**，
   并照常产生延迟副作用。
2. **`timeout`（Jupyter）与 `hard_timeout`（shell）属于同一类问题**：都只约束 API 等待，
   都不终止容器内的执行。
3. **"临时文件 + shell + `hard_timeout`" 方案被证伪**，不应作为
   `SandboxClient.execute_python` 的硬超时实现。
4. 逃逸面甚至更大：普通子进程、`start_new_session=True` 的脱离进程、shell `&` 后台任务
   在超时返回后都仍然存活。
5. Harness 层现有保护仍然成立：`execution_uncertain` 使工具错误成为终止性错误，任务
   不会重放（见 `06_harness_v1.md` 第 5.3 节）。缺口在沙箱层，不在 Harness 决策层。

---

## 10. 决策与后续

按 [`06_harness_v1.md`](06_harness_v1.md) 第 7 节与本次判定门槛：

- **不接入生产代码**；`SandboxClient.execute_python` 保持不变；
- **P0-5 继续保持未完成**；
- 下一步转向会话/容器级终止，优先探测 `cleanup_session(session_id)` 是否真正终止进程树
  （当前进程、普通子进程、`start_new_session` 脱离进程、shell `&` 后台进程），
  并测量"超时返回 → 进程消失"的窗口；
- 即使 `cleanup_session` 可用，硬边界也只能是
  `timeout + API 返回开销 + cleanup_session 延迟`，**不能称为精确硬超时**。

迁移前仍需完成的前置审计：`document_tool`、`sandbox/hooks` 对 Jupyter `display_data`、
富输出与会话状态的依赖。
