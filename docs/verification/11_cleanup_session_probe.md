# cleanup_session 能力探针：会话级终止能否胜任硬超时

> 记录日期：2026-09-28
> 实现分支：`main`
> 探针脚本：[`demo/cleanup_session_probe.py`](../../demo/cleanup_session_probe.py)
> 当前状态：**探测完成，判定不通过；停止该路线**

前置结论见 [`10_hard_timeout_probe.md`](10_hard_timeout_probe.md)：`timeout`（Jupyter）与
`hard_timeout`（shell）都只约束 API 等待，不终止容器内执行。本页验证剩下的候选——
`cleanup_session(session_id)` 是否能真正终止进程树。

本页不修改生产执行链路。

---

## 1. 验收对象

| 项目 | 值 |
| --- | --- |
| 分支 | `main` |
| Docker Client / Server | 29.7.2 / 29.7.2（Docker Desktop 4.87.0） |
| 沙箱镜像 | `...all-in-one-sandbox:1.11.0` |
| 镜像 image ID | `sha256:6328d7fd2f0ff0b4c147c3d05b3df1ce331f4a482eb6e550ecd64ed1fcf906e7` |
| `agent-sandbox` SDK | `0.0.30` |
| 沙箱地址 / 工作区 | `http://localhost:8080` / `/home/gem/workspace` |
| 会话模型 | `create_session(id=...)` 显式建会话，`exec_command(id=...hard_timeout=...)` 执行，`cleanup_session(id)` 销毁 |
| 轮询参数 | 消失判定 `VANISH_POLL_SECONDS = 0.25`，上限 `VANISH_DEADLINE_SECONDS = 20` |

复现命令：

```powershell
.venv\Scripts\python.exe -m demo.cleanup_session_probe
```

---

## 2. 场景与判定标准

| # | 场景 | 类型 |
| --- | --- | --- |
| 1 | 当前进程（sleep 3s → 写文件 → 存活 30s），`hard_timeout=1s` | required |
| 2 | 普通子进程（父进程等待子进程，两者都存活） | required |
| 3 | `start_new_session=True` 脱离进程组 | boundary |
| 4 | shell `&` 后台任务（API 不超时，只有 cleanup 能停） | required |
| 5 | 旁观会话隔离：另一会话中的进程不应被误杀 | 判定项 |
| 6 | 会话生命周期：清理后是否失效、能否重建、未知 session 的返回 | 判定项 |

判定标准（用户给定）：

```diff
API 返回超时
+ cleanup_session 完成
+ 目标进程树为 0
+ 延迟副作用文件不存在
+ 其他 session 不受影响
```

---

## 3. 原始输出

```text
sandbox base_url : http://localhost:8080
workspace        : /home/gem/workspace
probe root       : /home/gem/workspace/cleanup-session-probe-914f27d2
scenarios        : 4

--- current-process sleep=3s linger=30s hard_timeout=1s [required] ---
  expectation      : cleanup must terminate the command that timed out
  api              : success=True status='hard_timeout' exit_code=-1 api_seconds=1.07
  cleanup          : {'success': True, 'message': 'Session htclean-session-current-2253696c cleaned up successfully'} (0.00s)
  alive            : before_cleanup=1 after_cleanup=0 vanish_after_timeout=0.71s total_boundary=1.78s
  delayed marker   : absent
  reuse session    : {'raised': "ApiError: ... status_code: 404, body: {'success': False, 'message': 'Session not found', ...}"}
  verdict          : CONTAINED

--- ordinary-child sleep=3s hard_timeout=1s [required] ---
  expectation      : cleanup must also terminate the child process
  api              : success=True status='hard_timeout' exit_code=-1 api_seconds=1.07
  cleanup          : {'success': True, 'message': 'Session htclean-session-child-4584139e cleaned up successfully'} (0.00s)
  alive            : before_cleanup=2 after_cleanup=0 vanish_after_timeout=0.64s total_boundary=1.71s
  delayed marker   : absent
  reuse session    : {'raised': "ApiError: ... status_code: 404, body: {'success': False, 'message': 'Session not found', ...}"}
  verdict          : CONTAINED

--- detached-child start_new_session=True sleep=3s hard_timeout=1s [boundary] ---
  expectation      : detached process group; likely escapes a session-level kill
  api              : success=True status='hard_timeout' exit_code=-1 api_seconds=1.07
  cleanup          : {'success': True, 'message': 'Session htclean-session-detached-8039fa2f cleaned up successfully'} (0.01s)
  alive            : before_cleanup=2 after_cleanup=0 vanish_after_timeout=2.52s total_boundary=3.58s
  delayed marker   : present
  reuse session    : {'raised': "ApiError: ... status_code: 404, body: {'success': False, 'message': 'Session not found', ...}"}
  verdict          : LEAKED

--- shell-background sleep=3s linger=30s [required] ---
  expectation      : cleanup must stop a background job started in the session
  api              : success=True status='completed' exit_code=0 api_seconds=0.19
  cleanup          : {'success': True, 'message': 'Session htclean-session-background-59504b4c cleaned up successfully'} (0.00s)
  alive            : before_cleanup=1 after_cleanup=0 vanish_after_timeout=0.65s total_boundary=0.83s
  delayed marker   : absent
  reuse session    : {'raised': "ApiError: ... status_code: 404, body: {'success': False, 'message': 'Session not found', ...}"}
  verdict          : CONTAINED

=== isolation ===
  bystander        : {'create': True, 'alive_before': 1, 'alive_after_target_cleanup': 1}
=== session lifecycle ===
  first scenario reuse after cleanup : 404 Session not found
  cleanup unknown session            : {'success': False, 'message': 'Session htclean-missing-54b00e02 not found'}
  rebuild new session                : {'create_success': True, 'message': 'Session created successfully', 'status': 'completed', 'output': 'rebuilt-ok'}
  recreate cleaned session id        : {'create_success': True, 'message': 'Session created successfully', 'status': 'completed', 'output': 'rebuilt-ok'}
=== summary ===
required contained : 3/3
boundary contained : 0/1
boundary leak      : detached-child start_new_session=True sleep=3s hard_timeout=1s
total boundary (timeout -> processes gone, s): min=0.83 max=3.58

=== decision ===
cleanup_session did NOT contain every scenario.
Stop this route; move to one-shot container evaluation.
exit_code=1
```

（`ApiError` 的 HTTP 头已在本文中省略，原始字段为
`status_code: 404`、`body: {'success': False, 'message': 'Session not found', 'data': None}`。
两次独立运行的数值几乎一致，结果可复现。）

---

## 4. 结果汇总

| 场景 | status | api_seconds | cleanup_seconds | 清理前存活 | 超时后消失耗时 | 总边界 | 延迟副作用文件 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 当前进程 | `hard_timeout` | 1.07s | 0.00s | 1 | 0.71s | **1.78s** | 不存在 | CONTAINED |
| 普通子进程 | `hard_timeout` | 1.07s | 0.00s | 2 | 0.64s | **1.71s** | 不存在 | CONTAINED |
| 脱离进程组 `start_new_session=True` | `hard_timeout` | 1.07s | 0.01s | 2 | **2.52s** | **3.58s** | **存在** | **LEAKED** |
| shell `&` 后台任务 | `completed` | 0.19s | 0.00s | 1 | 0.65s | **0.83s** | 不存在 | CONTAINED |

**required 3/3 通过，boundary 0/1 通过；整体判定不通过。**

---

## 5. 判定标准逐条核对

| 判定项 | 结果 | 说明 |
| --- | --- | --- |
| API 返回超时 | 通过 | 三个超时场景均返回 `status='hard_timeout'`（后台场景按设计不超时） |
| `cleanup_session` 完成 | 通过 | 四个场景均返回 `success=True`，耗时 ≤ 0.01s |
| 目标进程树为 0 | 通过（但**不是即时**的） | 四个场景最终都归零；脱离场景用了 2.52s |
| 延迟副作用文件不存在 | **不通过** | 脱离进程组场景文件存在 |
| 其他 session 不受影响 | 通过 | 旁观会话进程在目标会话清理前后均为 1 |

按既定门槛：**条件未全部满足，停止 `cleanup_session` 路线，转向一次性容器方案。**

---

## 6. 时间窗口（必须记录的硬边界）

`cleanup_session` 只有在"超时返回之后"才被调用，因此它能提供的最强保证是：

```text
timeout + API 返回开销 + cleanup_session 延迟 + 进程真正消失的延迟
```

本次实测这段窗口：

| 场景 | 请求超时 | 总边界（发起 → 进程消失） | 超出超时值的部分 |
| --- | --- | --- | --- |
| 当前进程 | 1.0s | 1.78s | **+0.78s** |
| 普通子进程 | 1.0s | 1.71s | **+0.71s** |
| 脱离进程组 | 1.0s | 3.58s | **+2.58s** |
| shell `&` 后台任务 | 无超时 | 0.83s | — |

窗口区间为 `0.83s ~ 3.58s`。**即使某个场景被判定为 CONTAINED，也不能称为"精确硬超时"**：
真实边界至少是 `timeout + 0.7s`，脱离进程组场景为 `timeout + 2.6s`，且这段窗口内的
副作用已经可以落地（脱离场景正是如此：进程在 cleanup 后仍存活约 2.5s，并在 t≈3.0s
写出了文件）。

---

## 7. 会话生命周期

| 检查 | 结果 |
| --- | --- |
| 清理后用原 `session_id` 执行命令 | `ApiError`，HTTP 404，`Session not found` → **会话已失效** |
| 清理不存在的 session | `success=False`，`message='Session <id> not found'`（不抛异常） |
| 用新 id 重建会话 | `create_session` 成功，`echo rebuilt-ok` 正常 |
| 复用已清理会话的 id 重建 | `create_session` 成功，`echo rebuilt-ok` 正常 → **id 可复用** |

结论：会话的销毁与重建是干净、确定的；`cleanup_session` 的语义本身没有问题，
问题在于它**不是同步终止**，且挡不住脱离进程组的派生进程。

---

## 8. 探针自身的残留检查

```text
$ docker exec doc-agent-sandbox sh -c "ls -a /home/gem/workspace; ps -ef | grep -E 'htclean|htprobe' | grep -v grep | wc -l"
...（工作区仅剩原有文件）...
--- tagged processes ---
0
```

探针目录已删除，`htclean-` / `htprobe-` 残留进程为 0。

---

## 9. 结论

1. `cleanup_session(session_id)` **确实能终止会话内的进程**，包括普通子进程和 shell `&`
   后台任务，且不误杀其他会话，清理耗时接近 0。
2. 但它**既不即时、也不完备**：
   - 进程真正消失发生在 cleanup 返回之后（本次 0.64s ~ 2.52s）；
   - `start_new_session=True` 脱离进程组的派生进程会在窗口内继续运行并产生副作用。
3. 因此它**不能**作为 `execute_python` 的硬超时实现，按既定门槛停止该路线。
4. 下一步：评估**一次性容器**方案（每次执行使用独立容器，超时后直接销毁容器）。
   该方案的具体实现按本轮要求**尚未评估**。
5. 迁移前仍需完成前置审计：`document_tool`、`sandbox/hooks` 对 Jupyter `display_data`、
   富输出与会话状态的依赖。

`SandboxClient.execute_python` 未做任何修改；P0-5 继续保持未完成。
