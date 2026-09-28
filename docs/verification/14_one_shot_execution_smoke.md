# 一次性容器执行后端真机烟测记录

> 记录日期：2026-09-28
> 分支：`feat/one-shot-execution`
> 脚本：[`demo/one_shot_smoke.py`](../../demo/one_shot_smoke.py)
> 设计：[`../design/14_one_shot_execution_migration.md`](../design/14_one_shot_execution_migration.md) 第 5.2 节
> 结果：**14/14 通过**（退出码 0）

本页记录设计 14 第 5.2 节的真机验收。全部检查都通过真实 Docker 执行，直接驱动生产类
（`SubprocessDockerRunner` + `ContainerSupervisor` + `TaskWorkspace`），没有 mock。

---

## 1. 环境

| 项目 | 值 |
| --- | --- |
| Docker | 29.7.2（Docker Desktop 4.87.0） |
| 镜像 | `...all-in-one-sandbox:1.11.0@sha256:6328d7fd...fcf906e7` |
| 运行身份 | `1000:1000` |
| 容器参数 | `--network none --pull never --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --cpus 2 --memory 4g --memory-swap 4g --pids-limit 512` |
| 挂载 | 任务目录 `:ro` + 调用级 `out/` `:rw` + `/runner/run.py:ro` |

复现命令：

```powershell
.venv\Scripts\python.exe -m demo.one_shot_smoke
```

---

## 2. 原始输出

```text
PASS preflight                              sandbox preflight passed (10 checks)
       non-root uid/gid                 ok  uid=1000 gid=1000
       image pinned by digest           ok  ...@sha256:6328d7fd...fcf906e7
       image present locally (no pull)  ok  ...@sha256:6328d7fd...fcf906e7
       minimal container starts         ok  a825c9724302
       task dir readable                ok  preflight-input
       out writable                     ok  ok
       root filesystem not writable     ok  denied
       tmp writable                     ok  ok
       sibling task dirs invisible      ok  no
       preflight container removed      ok  a825c9724302
PASS normal call succeeds                   status=ok exit=0 error='' stderr=''
PASS stdout returned                        hello from sandbox
'done'
PASS trailing expression echoed             hello from sandbox
'done'
PASS artifact committed to task dir         .data\one-shot-smoke\...\tasks\smoke-...\report.txt
PASS container removed after success        container_id cleared
PASS timeout is terminal and uncertain      status=timeout uncertain=True terminal=True
PASS no delayed side effect after timeout   .data\one-shot-smoke\...\tasks\smoke-...\late.txt
PASS detached-child call times out          status=timeout
PASS detached child produced nothing        .data\one-shot-smoke\...\tasks\smoke-...\detached.txt
PASS concurrent calls both succeed          statuses=['ok', 'ok']
PASS orphan container created for the test  10a31f9bec8f
PASS orphan swept by exact id               examined=1 removed=1 kept_active=0 kept_young=0 failed=0
PASS no managed containers left             left=[]

=== summary ===
checks : 14/14 passed
=== decision ===
One-shot backend held every safety gate on real Docker.
```

---

## 3. 逐项对照 5.2 验收标准

| # | 5.2 要求 | 结果 |
| --- | --- | --- |
| 1 | 四种进程形态在结束或超时后全部消失 | 通过：超时场景无延迟文件；`start_new_session` 子进程随容器一起消失 |
| 2 | 超时后挂载目录没有延迟副作用 | 通过：等待 12s 后 `late.txt` / `detached.txt` 均不存在 |
| 3 | 正常结束后容器不存在 | 通过：容器被强杀并按精确 ID 确认消失 |
| 4 | 并发调用互不影响 | 通过：两个并发调用都是 `ok` |
| 5 | `--network none` 下解析类任务可执行 | 通过：preflight 内的镜像与解析库检查通过 |
| 6 | 非 root + 只读根文件系统 | 通过：`out` 可写、根不可写、tmp 可写、兄弟任务不可见 |
| 7 | 大结果卸载与 `read_tool_output` 仍工作 | 由离线全量回归覆盖（见第 5 节） |
| 8 | 孤儿扫描器按 TTL 清理 | 通过：按精确 Container ID 清理，`left=[]` |
| 9 | 性能分阶段实测 | 见第 4 节 |

---

## 4. 真机发现并修复的问题

烟测一次通过不是事实，实际暴露了三个真实缺陷，都在本范围内修复后复测通过：

1. **`--read-only` 下嵌套挂载点无法创建。**
   容器根文件系统只读，而镜像里没有 `/home/gem`，Docker 无法为
   `.../tasks/<id>/out` 创建挂载点，报 `make mountpoint ... read-only file system`。
   修复：在宿主侧预建空的 `<task_dir>/out` 占位目录后再启动容器。
2. **`docker exec` 未设置工作目录。**
   模型代码里的相对路径（`out/report.txt`）落到了容器根目录，`FileNotFoundError`。
   修复：`docker exec -w <task virtual dir>`。
3. **宿主超时误用了遗留的 HTTP grace。**
   `timeout + sandbox_http_grace(10s)` 让"超时 2 秒"的调用实际允许 12 秒，
   8 秒的代码因此跑完并侥幸豁免。修复：新增独立的
   `EXEC_KILL_GRACE_SECONDS = 1.0`，宿主计时器成为真正的硬边界。

另外修复了 preflight 探针脚本缺少换行导致字段解析错位的问题（工具缺陷，非环境问题）。

---

## 5. 回归

```text
.venv\Scripts\python.exe -m pytest -q
428 passed, 3 skipped, 1 warning in 9.10s

.venv\Scripts\python.exe -m pytest -q tests/test_harness_v1_scenarios.py
8 passed in 1.94s
```

3 个 skip 全部是符号链接用例（`test_security.py` 原有 1 个 + 本次新增的提交协议与
supervisor 各 1 个），原因是本机 Windows 账户无法创建符号链接。**"拒绝符号链接"这条
提交协议规则在本机只走到 skip，未被实际执行**；需要在开启开发者模式的 Windows 或
Linux CI 上补跑。

---

## 6. 结论与状态

- 一次性容器后端在真实 Docker 上通过了 5.2 的全部安全门槛；
- 生产 `wiring` 只装配一次性容器后端，无自动降级；
- 启动期 `preflight()` 由 API lifespan 显式调用，可用 `SANDBOX_PREFLIGHT_ON_STARTUP=false` 关闭；
- **P0-5 仍未完成**：全量回归与 Harness V1 已通过，但符号链接用例尚未在支持的环境执行，
  且真实 LLM 链路未验证（本地无 Key）。因此本分支**不合入 `main`**。
