# Design Note 14：一次性容器执行链路（迁移成本核算 + 设计）

> 状态：**已冻结（2026-09-28），未实现**
> 冻结记录：第一次评审为有条件通过（7 项修订），第二次评审确认达到冻结标准；
> 实现分支：`feat/one-shot-execution`
> 前置真机探针：**已完成，15/15 通过**（见第 8 节）；其中"镜像内存在 `gem` 用户"的
> 假设被推翻，运行身份改为固定 `1000:1000`
> 上游证据：[`../verification/12_one_shot_container_probe.md`](../verification/12_one_shot_container_probe.md)、
> [`../verification/13_jupyter_dependency_audit.md`](../verification/13_jupyter_dependency_audit.md)
> 本文不改动任何生产代码；评审通过后才建立 `feat/one-shot-execution` 分支。

---

## 0. 决策背景

现有沙箱内的两条终止路线均被实测排除：`hard_timeout` 只终止 API 等待（8/8 泄漏），
`cleanup_session` 既不即时也不完备（脱离进程组场景泄漏）。一次性容器是唯一同时满足
"进程必被终止"与"副作用不发生"的方案。

已定决策：**每次代码工具调用一个一次性容器**；**`--network none` + 宿主侧 `docker exec`**。
每次调用独立容器，而不是每任务一个，是因为任务级容器仍允许一次调用"成功返回但留下后台
进程"，污染后续调用。

---

## 1. 评审结论与冻结决策

第一次评审结论为**有条件通过**：容器粒度、`--network none` + 宿主 `docker exec`、
只读输入与隔离输出、错误与超时映射均通过；**双后端自动降级被否决**；文档需按 7 项
修改意见修订后再建分支。

本节记录评审已冻结的 5 项决策（连同 7 项修订，落实在第 3 节）。

| # | 议题 | 决定 |
| --- | --- | --- |
| 1 | 旧 HTTP 后端降级 | **不保留自动降级路径**。生产 `wiring` 只装配一次性容器后端；旧类可保留用于差异测试，但不提供运行期 fallback；需要回滚时用 Git 版本回滚，而不是降低安全等级。P0-5 完成前删除遗留生产接线。 |
| 2 | 镜像 | 第一版继续使用 AIO Sandbox 镜像，固定为 `...all-in-one-sandbox:1.11.0@sha256:6328d7fd2f0ff0b4c147c3d05b3df1ce331f4a482eb6e550ecd64ed1fcf906e7`（前置探针已确认 manifest digest 与本地 image ID 一致）；覆盖 entrypoint，不启动原有 HTTP/Jupyter 服务；本次不裁剪专用镜像。镜像内 Python 为 **3.10.12**，与开发虚拟环境 3.13 不同，需在工具契约中说明。 |
| 3 | 运行用户 | **前置探针推翻了"镜像内有 `gem` 用户"的假设**：镜像内不存在 `gem` 账户，`/home/gem` 也不存在（原先是 entrypoint 与 compose volume 造成的）。改为固定 `1000:1000`——实测只有该 UID 能写嵌套 `out` 挂载，`65534:65534` 与 `0:0` 均被拒绝。Windows Docker Desktop 的写权限由**宿主文件共享身份**决定，容器内 `stat` 显示的 `0:0 777` 不代表真实门禁。 |
| 4 | 只读根文件系统 | **第一版即启用** `--read-only --tmpfs /tmp`，并把 `HOME` / `XDG_CACHE_HOME` / `MPLCONFIGDIR` 重定向到 `/tmp`。 |
| 5 | 冷启动性能 | 实现分支必须重测：镜像已存在时 20–30 次的 P50/P95、Docker Desktop 刚启动后的首次调用、各阶段耗时、首次拉取单独统计；生产执行使用 `--pull never`。 |

---

## 2. 迁移成本核算

### 2.1 受影响模块清单

| 模块 | 变更性质 | 预估规模 | 风险 |
| --- | --- | --- | --- |
| `sandbox/client.py` | **大改**：`execute_python` 改为宿主侧容器执行；文件操作改为宿主本地工作区；路径守卫迁到宿主 | 400–600 行 | 高 |
| 新增 `sandbox/container_supervisor.py` | 创建 / exec / 强杀 / **按精确 Container ID 验证消失** / 标签 / 就绪 | 280–380 行 | 中 |
| 新增 `sandbox/call_workspace.py` | 调用级目录、只读输入挂载、控制目录、**产物提交协议** | 220–300 行 | 中-高 |
| 新增 `sandbox/exec_wrapper.py` | 容器内包装脚本：AST 末行回显、异常捕获、Agg 后端、flush | 80–120 行 | 中 |
| 新增 `sandbox/orphan_sweeper.py` | 标签扫描 + TTL 清理 | 80–120 行 | 低 |
| `tools/document_tool.py` | **确定性改造**：读任务目录、写 `out/<name>.md`、路径改写（见第 4 节） | 60–80 行 | 中 |
| `tools/sandbox_tool.py` | 工具描述改为"必须 `print()`"、错误映射 | ~30 行 | 低 |
| `sandbox/hooks.py`、`sandbox/inputs.py` | 适配宿主工作区语义 | ~80 行 | 低-中 |
| `agent/wiring.py` | 组装 supervisor / workspace / sweeper，移除 HTTP 后端接线 | ~50 行 | 低 |
| `core/config.py` | 新增容器配置（镜像摘要、data root、UID/GID、超时、spool 上限、提交配额、TTL） | ~80 行 | 低 |
| `agent/executor.py` | System Prompt 增加"显式 `print`、产物写入 `out/`" | ~10 行 | 低 |
| 测试 | 新增 supervisor / workspace / 提交协议 / wrapper / sweeper 单测 + 真机烟测；重写 `test_sandbox_client.py`、`test_security.py` 相关用例 | 900–1300 行 | 中-高 |
| 文档 | design 03 / 09 / 10、README、`.env.example`、`docker-compose.yaml` 说明 | —— | 低 |

### 2.2 现有测试面的迁移成本

含 sandbox / `SandboxClient` 引用的测试文件共 **12 个**，语义最集中的是：

| 测试文件 | 用例数 | 迁移影响 |
| --- | --- | --- |
| `tests/test_security.py` | 26 | 会话创建/删除配对、超时终止、清理失败断言需按新后端重写 |
| `tests/test_sandbox_client.py` | 12 | 输出归一化、timeout 透传需重写 |
| `tests/test_document_tool.py` | 12 | 输出路径契约变化，解析逻辑不变 |
| `tests/test_input_stager.py` | 10 | 输入装载路径语义变化 |
| `tests/test_concrete_tools.py` | 4 | 依赖 `ExecutionResult` 形状 |
| `tests/test_sandbox_hooks.py` | 3 | `execute_in_sandbox` 返回契约不变 |

### 2.3 工作量估算（单工程师）

| 阶段 | 估算 |
| --- | --- |
| 设计冻结 + 接口定义（含 `ExecutionResult` 兼容层） | 0.5 天 |
| `container_supervisor` + `call_workspace`（含提交协议） + `exec_wrapper` + sweeper | 3–4 天 |
| 接线、工具改造（含 `parse_document` 确定性改造）、提示词与配置 | 1.5–2 天 |
| 测试（单测 + 真机烟测 + 全量回归） | 3–4 天 |
| 文档与验收记录 | 1 天 |
| **合计** | **9–11.5 个工程师日** |

（较首版 6.5–9 天上调：新增产物提交协议、非 root 与只读根文件系统的真机验证、
精确 ID 验证、输出 spool，以及更多测试面。）

### 2.4 性能成本（已按评审修正）

固定额外延迟必须包含"创建并启动 + 正常结束后的强杀 + 验证消失"：

```text
容器创建并启动（缓存命中实测）   ≈ 0.78s
+ docker rm -f                   ≈ 0.50s
+ 验证消失（inspect 轮询）        ≥ 0.05s
+ 产物提交（有产物时）            视文件数而定
──────────────────────────────────────────
固定额外延迟                     ≥ 1.33s / 次
```

因此 5 次代码调用的任务至少增加 **6.65s**，而不是首版写的 4s。该数值仍需在实现分支用
**最终配置**（`--read-only` + `--tmpfs /tmp` + 非 root + `--network none` +
`--entrypoint sleep infinity`）重新实测，并按 1 号决策表第 5 项分阶段记录。

**前置探针实测（最终配置，Windows Docker Desktop 29.7.2）：**

| 阶段 | 实测 |
| --- | --- |
| keepalive 容器创建并启动 | **0.36s** |
| `docker rm -f` | **0.23s** |
| 按精确 ID 轮询确认真实消失 | **0.42s** |
| 固定开销合计（不含 exec 与产物提交） | **≈1.01s / 次** |

5 次代码调用的固定开销约 **5s**（首版 1.33s/次 的估算偏保守）。exec 执行时间与产物提交
时间取决于任务本身，不计入固定开销。

### 2.5 成本之外的真实风险

1. **末行表达式语义**：当前 System Prompt 未要求 `print`，迁移后会静默失效；
2. **路径与提交契约**：模型必须知道产物写入 `out/`，且提交规则要能挡住符号链接与越界；
3. **错误映射**：新后端要自己定义一套干净的映射，不能沿用旧的两套表示。

---

## 3. 生产执行链路设计

### 3.1 组件划分

```text
可信宿主进程（API / Worker）
├── ContainerSupervisor   容器生命周期：create / exec / rm -f / verify-gone(by ID)
├── CallWorkspace         调用级目录、只读输入、控制目录、产物提交
├── ExecWrapper(模板)     容器内脚本：AST 回显、异常捕获、Agg 后端、flush
└── OrphanSweeper         标签扫描 + TTL 清理（启动时 + 周期）

容器（一次性、network none、read-only rootfs、非 root、受限资源）
└── 仅执行 ExecWrapper，不持有任何 Docker 控制能力
```

### 3.2 路径与挂载（按评审修订）

**保持现有规范虚拟路径不变**，不做 `/workspace` 重映射：

```text
<host_task_dir>            → /home/gem/workspace/tasks/<task_id>        (ro)
<call>/out                 → /home/gem/workspace/tasks/<task_id>/out    (rw)
<call>/control/run.py      → /runner/run.py                             (ro)
```

这样输入文件的绝对路径、`cwd`、`PermissionManager` / `ToolRegistry` / `InputStager`
的路径校验、以及任务隔离契约基本不变，只新增一条 `out/` 输出规则。

同级任务的目录不会出现在容器内（每个容器只挂载本任务目录），任务间不可见性由挂载边界保证。

### 3.3 控制文件与产物分离（按评审修订）

包装脚本**不放在产物目录**，否则成功提交会把内部脚本当成产物：

```text
<call>/control/run.py   → /runner/run.py       (ro)   控制面，永不参与合并
<call>/out/             → .../tasks/<id>/out   (rw)   唯一的产物来源
```

备选：通过 stdin 传入用户代码。无论采用哪种方式，控制文件都在合并范围之外，并列入
"内部保留名"拒绝清单。

### 3.4 单次调用流程

```text
1. 准备调用目录      <data>/calls/<call_id>/{control,out,logs}
2. 创建一次性容器    docker run -d --name doc-agent-call-<call_id>
                       --label doc-agent.managed=1
                       --label doc-agent.task_id=<task_id>
                       --label doc-agent.tool_call_id=<call_id>
                       --network none --pull never
                       --read-only --tmpfs /tmp
                       --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges
                       --cpus 2 --memory 4g --memory-swap 4g --pids-limit 512
                       --entrypoint sleep <image>@<digest> infinity
                       -v <host_task_dir>:/home/gem/workspace/tasks/<task_id>:ro
                       -v <call>/out:/home/gem/workspace/tasks/<task_id>/out:rw
                       -v <call>/control/run.py:/runner/run.py:ro
                       -e HOME=/tmp -e XDG_CACHE_HOME=/tmp -e MPLCONFIGDIR=/tmp
                    记录返回的精确 Container ID
3. 宿主侧执行        docker exec -i <container_id> python3 /runner/run.py
                     （宿主计时器负责超时；不信任 exec 自身的 timeout）
4. 正常完成          收集 stdout/stderr（流式写日志文件）+ 产物
5. 超时              立即 docker rm -f <container_id>
6. 验证消失          轮询 docker inspect <container_id> 直到 NotFound（见 3.7）
7. 提交产物          成功：按 3.5 协议提交；失败：丢弃 call 目录
8. 返回结果          ok / error / timeout(execution_uncertain=true, terminal=true)
9. finally 兜底      容器仍存在则再 rm -f；失败调用清理调用目录
```

### 3.5 产物提交协议（按评审补全）

提交前**先扫描、后提交**，任何校验失败都不得开始写入：

| 规则 | 内容 |
| --- | --- |
| 允许的条目 | 仅普通文件与允许的目录层级（默认单层；嵌套需显式开启） |
| 拒绝的条目 | 符号链接、设备文件、FIFO、套接字，以及指向 `out/` 之外的链接 |
| 路径校验 | 对每个条目取真实路径，确认仍在 `out/` 内；提交前后各校验一次最终路径 |
| 冲突策略 | **默认拒绝覆盖**任务目录中已存在的文件；如需覆盖必须显式配置，且只允许"目标不存在"或"内容逐字节相同"两种情况通过 |
| 配额 | `max_files`（默认 64）、`max_total_bytes`（默认 256 MB）、单文件上限 |
| 保留名 | `run.py`、`control/`、`stdout.log`、`stderr.log` 等内部名一律拒绝 |
| 写入原子性 | 单文件使用同目录临时文件 + `os.replace` |
| 中途失败 | 按 `execution_uncertain=true` / `terminal=true` 终止，**绝不把部分结果报告为成功**；事件中记录已提交文件清单，避免模型误判任务已完成 |
| 路径改写 | `metadata` 中的临时路径（如 `parse_document` 的 `markdown_path`）统一改写为提交后的最终路径 |

> 多文件提交无法做到整体原子性。因此协议选择"逐文件原子 + 失败即终止 + 明确记录已提交
> 清单"，而不是假装可以整体回滚。

### 3.6 输出捕获与 P0-3 的衔接（按评审修正）

首版"1 MB 截断 + 仍由 P0-3 完整回读"是自相矛盾的，现修正为：

1. supervisor 把 stdout/stderr **流式写入** `<call>/logs/stdout.log`、`stderr.log`，
   并设置一个**明确的 spool 上限**（默认 32 MB，可配置）；
2. 未超过 spool 上限时，工具结果文本完整；超长部分继续由现有
   `AfterToolCallHook` 写入 `ToolOutputStore`，模型只收到 preview + `result_ref`，
   通过 `read_tool_output` 分页回读**完整内容**；
3. 超过 spool 上限时，丢弃超出部分并设置 `metadata.output_truncated=true` 与可见标记，
   **明确声明超出部分不可回读**——文档不再声称 P0-3 能兜住被截断的内容；
4. 图片类输出不再进入上下文（旧实现会 `str(data)` 成 base64），改为落盘 + `save_report`。

### 3.7 容器消失验证（按评审修订）

- 不使用 `docker ps -f name=<container>`（名称过滤可能模糊匹配）；
- `docker run` 后保存**精确 Container ID**；
- `docker rm -f` 后轮询 `docker inspect <container_id>`，直到明确返回 NotFound；
- 超过清理期限仍未确认消失 → 按 `execution_uncertain=true` / `terminal=true` 终止任务。

### 3.8 错误映射

| 场景 | 处理 |
| --- | --- |
| 退出码 0 | `status="ok"` |
| 退出码非 0 | `status="error"`，返回 stderr + traceback |
| 宿主计时器超时 | 强杀 → 按 ID 验证消失 → `status="timeout"`、`execution_uncertain=true`、`terminal=true` |
| 容器意外消失 / `rm -f` 失败 / 无法验证消失 | `execution_uncertain=true`、`terminal=true`，任务终止且不重放 |
| 产物提交失败 | 同上，按不确定终止 |

### 3.9 容器内包装脚本（ExecWrapper）

1. **AST 末行回显**：最后一条顶层语句若为 `ast.Expr`，改写为赋值并在执行后
   `print(repr(value))`（`None` 不打印），等价于 `execute_result` 语义；
2. **异常捕获**：traceback 写 stderr，退出码 1；
3. **无界面后端**：注入 `matplotlib.use("Agg")`，图必须 `savefig` 到 `out/`；
4. **flush**：显式 flush，避免强杀时丢失已产生的日志。

### 3.10 安全不变量

1. Docker 控制面只在宿主侧；容器内无 `/var/run/docker.sock`、无 `docker` 可执行文件
   （12 号探针已实测确认）。
2. `--network none`，容器不可出网、不可被外部访问。
3. `--read-only --tmpfs /tmp`，`HOME` / `XDG_CACHE_HOME` / `MPLCONFIGDIR` 指向 `/tmp`；
   若某解析库需要额外可写路径，只新增明确的 tmpfs，不放开整个根文件系统。
4. 固定非 root `1000:1000`（前置探针实测唯一可写 `out/` 的身份），每次调用前自检：
   输入可读、`out/` 可写、其他任务目录不可见、根文件系统不可写。
5. `--cap-drop ALL`、`no-new-privileges`、资源限制沿用已验收的
   `2 CPU / 4 GiB / swap=内存 / PIDs 512`。
6. 镜像固定摘要 + `--pull never`，启动阶段先验证镜像存在，避免任务触发不受控拉取。
7. 正常结束也删除容器，禁止复用。

### 3.11 无自动降级

生产 `wiring` 只装配一次性容器后端。旧 `SandboxClient` 的 HTTP 路径不会在运行期被
回退使用；需要回滚时用 Git 版本回滚。P0-5 完成前删除遗留生产接线。

### 3.12 孤儿容器管理

`OrphanSweeper` 在启动时与周期扫描 `label=doc-agent.managed=1`，对超过 TTL
（建议 `2 × sandbox_max_timeout`）或所属任务已非 RUNNING 的容器执行 `docker rm -f`，
并同样按精确 ID 验证消失。探针实测：标签扫描清理 2 个容器总耗时 1.25s。

---

## 4. `parse_document` 的确定性改造

当前 `DOCUMENT_SCRIPT` 把 Markdown 写在**当前工作目录**；任务根目录改为只读后该脚本
必然失败。这是确定性工具，不能依赖 System Prompt 提醒模型，必须由 `ParseDocumentTool`
自己保证：

```text
读取：任务目录中的源文件（只读挂载，绝对路径不变）
写入：out/<name>.md（唯一可写区）
成功后：metadata.markdown_path 指向提交后的最终路径
失败：不产生任何已提交文件
```

具体改动：脚本内 `markdown_path` 改为绝对路径 `<task_dir>/out/<name>.md`；
`ParseDocumentTool.execute` 在提交成功后把 `metadata["markdown_path"]` 从临时路径改写为
最终路径，并保持 `chars` / `truncated` 语义不变。

---

## 5. 验收标准（实现分支必须满足）

### 5.1 离线单测（fake docker runner，不需要 Docker）

- supervisor：创建/执行/强杀/**按 ID 验证消失**的调用序列与失败分支；
- call workspace：只读输入、控制目录隔离、调用目录生命周期；
- **产物提交协议**：允许/拒绝的条目类型、符号链接与越界拒绝、配额、冲突策略、
  逐文件原子替换、中途失败的 uncertain 终止与已提交清单记录；
- exec wrapper：AST 末行回显（含 `None` 不打印）、异常 → 退出码 1、Agg 注入；
- 输出 spool：未超限走 P0-3、超限标记 `output_truncated=true`；
- 错误映射四条路径；孤儿扫描 TTL 与任务状态判定。

### 5.2 真机烟测（必须真实 Docker）

1. 四种进程形态（主进程 / 普通子进程 / 后台 `&` / `start_new_session`）在结束或超时后
   全部消失（复用 12 号探针判定）；
2. 超时后挂载目录**没有**延迟副作用文件；
3. 正常结束后容器不存在（按精确 ID 验证）；
4. 并发多次调用互不影响；
5. `--network none` 下解析类任务仍可执行（PyMuPDF / openpyxl / python-pptx / docx2txt）；
6. 非 root + 只读根文件系统下，四项目检全部通过；Windows Docker Desktop 挂载权限行为
   单独记录结论；
7. 输出限长与 P0-3 卸载、`read_tool_output` 回读仍然工作；
8. 孤儿扫描器能在 TTL 内清理被遗弃容器；
9. 性能实测：各阶段耗时、P50/P95（20–30 次）、Docker Desktop 重启后首次调用、
   首次拉取单独统计。

### 5.3 全量回归

`python -m pytest -q` 全部通过（当前基线 `364 passed, 1 skipped`），
并重新执行 Harness V1 八条端到端场景。

### 5.4 实现验收点（第二次评审补充）

1. **排空管道**：stdout/stderr 超过 spool 上限（默认 32 MB）后虽然丢弃尾部，监督器仍必须
   持续把管道读干，避免子进程因管道写满而假死。
2. **幂等提交**：目标文件与来源内容逐字节相同时按幂等 no-op 处理，**不计为一次真正的覆盖**。
3. **清理竞态**：`OrphanSweeper` 在清理前必须确保不会与仍在执行的合法容器发生竞态
   （按标签、TTL 与任务状态三重判定），并且始终按精确 Container ID 验证消失。

---

## 6. 剩余待确认项

第 1 节的 5 项已冻结，实现前的两项环境探针已在前置探针中收口：

| # | 问题 | 结论 |
| --- | --- | --- |
| 1 | 镜像内 `gem` 用户的实际 UID/GID，以及 Windows 挂载下的写权限行为 | **已收口**：镜像内不存在 `gem` 用户；固定 `1000:1000` 可写嵌套 `out`，`65534` 与 `0` 均不可写 |
| 2 | 固定后的镜像摘要 | **已收口**：`sha256:6328d7fd2f0ff0b4c147c3d05b3df1ce331f4a482eb6e550ecd64ed1fcf906e7`（manifest digest 与本地 image ID 一致） |

---

## 7. 下一步

本页修订完成即冻结设计。评审通过后建立 `feat/one-shot-execution` 分支，按 5.1 → 5.2 →
5.3 推进；在此之前不修改生产代码，P0-5 保持未完成。

---

## 8. 前置真机探针记录（2026-09-28）

脚本：[`demo/one_shot_preflight_probe.py`](../../demo/one_shot_preflight_probe.py)
（分支 `feat/one-shot-execution`，诊断用途，不涉及生产代码）。

复现命令：

```powershell
.venv\Scripts\python.exe -m demo.one_shot_preflight_probe
```

结果：**15/15 检查通过**，退出码 0。

### 8.1 推翻的假设

设计冻结时假设镜像内存在 `gem` 用户（沿用 `/home/gem/workspace` 的直觉）。实测：

```text
$ docker run --rm --entrypoint sh <image> -c "id; grep gem /etc/passwd; ls -d /home/gem"
uid=0(root) gid=0(root) groups=0(root)
grep '^gem:' /etc/passwd -> 0 entries
ls: cannot access '/home/gem': No such file or directory
```

`/home/gem/workspace` 原先是**容器 entrypoint + compose volume** 造成的，不是镜像自带路径。
挂载仍然可用（Docker 会按需创建挂载点），但"运行身份"必须显式指定。

### 8.2 非 root 写入矩阵（嵌套 `out` 挂载）

挂载：`<host_task_dir> → /home/gem/workspace/tasks/<task_id>:ro`、
`<call>/out → .../tasks/<task_id>/out:rw`、`<call>/control/run.py → /runner/run.py:ro`，
容器参数 `--read-only --tmpfs /tmp --network none --cap-drop ALL --security-opt no-new-privileges`。

| 容器内 UID:GID | 写 `out/` | 写只读父目录 | 写根文件系统 | 写 `/tmp` | 解析库导入 |
| --- | --- | --- | --- | --- | --- |
| `1000:1000` | **ok** | denied | denied | ok | 4/4 ok |
| `65534:65534` | denied | denied | denied | ok | 4/4 ok |
| `0:0` | denied | denied | denied | ok | 4/4 ok |

容器内 `stat` 显示挂载点为 `0:0 777`，但真实门禁由**宿主文件共享身份**决定——只有
`1000:1000` 能写。这一点必须在实现中作为配置固定，并在每次调用前做写入自检。

### 8.3 其它实测值

| 项目 | 实测 |
| --- | --- |
| 只读父目录 + 可写子目录（嵌套挂载） | 生效（父目录写入被拒绝，`out/` 写入成功） |
| 只读根文件系统 | 生效（写 `/` 被拒绝） |
| `--tmpfs /tmp` | 可写 |
| 控制脚本 `:ro` 挂载并可执行 | 通过（`python3 /runner/run.py` → `runner-ok`） |
| 宿主可见产物 | `out/out-probe.txt` 出现在宿主目录 |
| 宿主不可见父目录写入 | `task/hack.txt` 未产生 |
| keepalive + `docker exec` | 通过（0.36s 启动，`exec-ok`） |
| 按精确 Container ID 验证消失 | 通过（`rm -f` 0.23s，验证 0.42s） |
| 镜像内 Python | **3.10.12** |
| 镜像默认用户 | 空（root） |

### 8.4 配置模型（据此冻结）

```text
sandbox_image       = .../all-in-one-sandbox:1.11.0@sha256:6328d7fd...
sandbox_uid_gid     = 1000:1000
sandbox_network     = none（--pull never）
sandbox_hardening   = --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges
sandbox_env         = HOME=/tmp XDG_CACHE_HOME=/tmp MPLCONFIGDIR=/tmp
sandbox_mounts      = <task_dir>:ro, <call>/out:rw, <call>/control/run.py:/runner/run.py:ro
```

> 探针同时修正了两个自身缺陷后才得到上述结论：写入目标最初误指向只读父目录；
> 容器消失检测最初用大小写敏感的 `"No such"`，而 CLI 实际返回小写
> `error: no such object:`。两者均已修正，最终结果可复现。
