# Design Note 14：一次性容器执行链路（迁移成本核算 + 设计）

> 状态：**设计提案，未实现**。本文不改动任何生产代码。
> 上游证据：[`../verification/12_one_shot_container_probe.md`](../verification/12_one_shot_container_probe.md)（一次性容器通过全部安全门槛）、
> [`../verification/13_jupyter_dependency_audit.md`](../verification/13_jupyter_dependency_audit.md)（Jupyter 依赖审计）
> 已定决策：**每次代码工具调用一个一次性容器**；**`--network none` + 宿主侧 `docker exec`**

---

## 0. 决策背景

两条在现有沙箱内"尝试终止执行"的路线都已被实测排除：`hard_timeout` 只终止 API 等待
（8/8 泄漏），`cleanup_session` 既不即时也不完备（脱离进程组场景泄漏）。一次性容器是唯一
同时满足"进程必被终止"和"副作用不发生"的方案。

容器粒度选择"每次代码工具调用一个"而不是"每任务一个"：任务级容器仍允许一次调用
"成功返回但留下后台进程"，污染后续调用。0.78s 冷启动对当前文档 Agent 可接受，安全优先。

网络选择 `--network none`：容器只能通过宿主侧 `docker exec` 驱动，Agent 与容器内代码
都无法触碰 Docker 控制面。

---

## 1. 迁移成本核算

### 1.1 受影响模块清单

| 模块 | 变更性质 | 预估规模 | 风险 |
| --- | --- | --- | --- |
| `sandbox/client.py` | **大改**：`execute_python` 改为"宿主侧容器执行"；文件操作改为宿主本地工作区（不再走 HTTP File API）；路径守卫迁到宿主 | 400–600 行 | 高 |
| 新增 `sandbox/container_supervisor.py` | 容器创建 / exec / 强杀 / 验证消失 / 标签 / 就绪 | 250–350 行 | 中 |
| 新增 `sandbox/call_workspace.py` | 调用级目录、只读输入挂载、产物合并、失败丢弃 | 150–200 行 | 中 |
| 新增 `sandbox/exec_wrapper.py` | 容器内包装脚本：AST 末行回显、异常捕获、Agg 后端、flush | 80–120 行 | 中 |
| 新增 `sandbox/orphan_sweeper.py` | 标签扫描 + TTL 清理 | 80–120 行 | 低 |
| `tools/sandbox_tool.py` | 工具描述改为"必须 `print()`"，错误映射 | ~30 行 | 低 |
| `tools/document_tool.py` | 适配新 cwd / 输出目录契约 | ~40 行 | 低 |
| `sandbox/hooks.py`、`sandbox/inputs.py` | 适配新工作区语义（宿主路径） | ~80 行 | 低-中 |
| `agent/wiring.py` | 组装 supervisor / workspace / sweeper | ~40 行 | 低 |
| `core/config.py` | 新增容器配置；沙箱 HTTP 配置转为遗留兼容项 | ~60 行 | 低 |
| `agent/executor.py` | System Prompt 增加"显式 `print`、产物写入 `out/`" | ~10 行 | 低 |
| 测试 | 新增 supervisor / workspace / wrapper 单测（fake docker）+ 真机烟测；重写 `test_sandbox_client.py`、`test_security.py` 相关用例 | 700–1000 行 | 中-高 |
| 文档 | design 03 / 09 / 10、README、`.env.example`、`docker-compose.yaml` 说明 | —— | 低 |

### 1.2 现有测试面的迁移成本

受影响（含 sandbox/SandboxClient 引用）的测试文件共 **12 个**，其中沙箱语义最集中的是：

| 测试文件 | 用例数 | 迁移影响 |
| --- | --- | --- |
| `tests/test_security.py` | 26 | 会话创建/删除配对、超时终止、清理失败断言需按新后端重写 |
| `tests/test_sandbox_client.py` | 12 | 四类输出归一化、timeout 透传需重写 |
| `tests/test_input_stager.py` | 10 | 输入装载路径语义变化 |
| `tests/test_document_tool.py` | 12 | 解析脚本执行路径变化，解析逻辑不变 |
| `tests/test_sandbox_hooks.py` | 3 | `execute_in_sandbox` 返回契约不变 |
| `tests/test_concrete_tools.py` | 4 | 依赖 `ExecutionResult` 形状，需适配 |

其余 6 个文件（`test_config` / `test_executor` / `test_orchestrator` / `test_task_manager` /
`test_task_model` / `test_task_scoping`）只是间接引用，预计改动很小。

### 1.3 工作量估算（单工程师）

| 阶段 | 估算 |
| --- | --- |
| 设计冻结 + 接口定义（含 `ExecutionResult` 兼容层） | 0.5 天 |
| `container_supervisor` + `call_workspace` + `exec_wrapper` | 2–3 天 |
| 接线、工具适配、提示词与配置 | 1–1.5 天 |
| 测试（单测 + 真机烟测 + 全量回归） | 2–3 天 |
| 文档与验收记录 | 1 天 |
| **合计** | **6.5–9 个工程师日** |

### 1.4 性能成本

- 每次代码工具调用增加 **≈0.78s** 冷启动（缓存命中实测值）；一个含 5 次代码调用的任务
  约增加 **4 秒**。首次拉取镜像或镜像变大后需要重测。
- 每次调用多一次容器创建 + 一次 `docker rm -f`（0.50s），属于可接受的固定开销。
- 文件操作从"HTTP File API"变为"宿主本地文件系统"，反而**减少**一次网络往返。

### 1.5 成本之外的真实风险

成本的主要部分不是代码量，而是行为回归：

1. **末行表达式语义**（`execute_result`）——当前 System Prompt 未要求 `print`，
   迁移后会静默失效（见审计 4.3）。必须用 AST 回显兜住，并同时改提示词。
2. **路径契约**——"相对路径解析到任务目录"的语义会被"只读输入 + 可写 `out/`"取代，
   模型必须被明确告知新契约。
3. **错误映射**——两条旧路径的失败表示本就不同（Jupyter `success=false`+`KernelError`
   vs Shell `success=true`+`hard_timeout`），新后端要自己定义一套干净的映射。

---

## 2. 生产执行链路设计

### 2.1 组件划分

```text
可信宿主进程（API / Worker）
├── ContainerSupervisor   容器生命周期：create / exec / rm -f / verify-gone
├── CallWorkspace         调用级目录、只读输入挂载、产物合并、失败丢弃
├── ExecWrapper(模板)     容器内脚本：AST 回显、异常捕获、Agg 后端、flush
└── OrphanSweeper         标签扫描 + TTL 清理（启动时 + 周期）

容器（一次性、network none、受限资源）
└── 仅执行 ExecWrapper，不持有任何 Docker 控制能力
```

### 2.2 单次调用的完整流程

```text
1. 准备调用目录      <data>/tasks/<task_id>/calls/<tool_call_id>/out
2. 创建一次性容器    docker run -d --name doc-agent-call-<tool_call_id>
                       --label doc-agent.managed=1
                       --label doc-agent.task_id=<task_id>
                       --label doc-agent.tool_call_id=<tool_call_id>
                       --network none
                       --cpus 2 --memory 4g --memory-swap 4g --pids-limit 512
                       --cap-drop ALL --security-opt no-new-privileges
                       --entrypoint sleep <image> infinity
                       -v <task_dir>:/workspace:ro
                       -v <call_out>:/workspace/out:rw
3. 宿主侧执行        docker exec -i <container> python3 /workspace/out/_run.py
                     （宿主计时器负责超时，不信任 exec 自身的 timeout）
4. 正常完成          收集 stdout/stderr（限长）+ 产物
5. 超时              立即 docker rm -f <container>
6. 验证消失          docker ps -a -q -f name=<container> 必须为空
7. 合并产物          成功：call_out/* → <task_dir>/；失败：丢弃 call_out
8. 返回结果          ok / error / timeout(execution_uncertain=true, terminal=true)
9. finally 兜底      容器仍在则再 rm -f；失败调用清理 call 目录
```

### 2.3 挂载与产物合并

```text
<data>/tasks/<task_id>/                    →  /workspace       (ro)   已提交的任务状态
<data>/tasks/<task_id>/calls/<call_id>/out →  /workspace/out   (rw)   本次调用唯一可写区
```

- **输入只读**：容器无法修改已提交状态，失败调用不可能留下半成品。
- **输出隔离**：模型把产物写到 `out/`；成功后由宿主把 `call_out/*` 合并到任务目录，
  下一次调用即可读取；失败或超时时整个 call 目录丢弃。
- **契约变化**：System Prompt 与工具描述必须明确"产物写入 `out/`"，这是迁移中最容易被
  忽略、也最容易造成静默失败的一处。
- 备选方案（不推荐）：把任务目录挂成 rw 并在失败时回滚。回滚需要快照/硬链接复制，
  复杂度和风险都高于"只读输入 + 调用级输出"。

### 2.4 容器内包装脚本（ExecWrapper）

职责单一，只做四件事：

1. **AST 末行回显**：若最后一条顶层语句是 `ast.Expr`，改写为赋值并在执行后
   `print(repr(value))`（`None` 不打印），等价于 Jupyter 的 `execute_result` 语义；
2. **异常捕获**：捕获异常 → traceback 写入 stderr → 退出码 1，供宿主映射为 error；
3. **无界面后端**：注入 `matplotlib.use("Agg")`，`plt.show()` 不再作为输出通道，
   图必须 `savefig` 到 `out/`；
4. **flush**：所有输出显式 flush，避免容器被强杀时丢失已产生的日志。

### 2.5 输出与错误处理

| 场景 | 处理 |
| --- | --- |
| 退出码 0 | `status="ok"`，返回合并后的 stdout/stderr 文本 |
| 退出码非 0 | `status="error"`，返回 stderr + traceback 文本 |
| 宿主计时器超时 | `docker rm -f` → 验证消失 → `status="timeout"`、`execution_uncertain=true`、`terminal=true` |
| 容器意外消失 / `rm -f` 失败 / 无法验证消失 | `execution_uncertain=true`、`terminal=true`，任务终止且不重放 |
| stdout/stderr 超长 | 在 supervisor 层设硬上限（如 1 MB）；完整大结果继续走 **P0-3 的 `ToolOutputStore` 卸载 + `read_tool_output` 回读**，不新增大结果机制 |
| 图片类输出 | 不再进入上下文（旧实现会 `str(data)` 成 base64），改为落盘 + `save_report` |

### 2.6 安全不变量

1. **Docker 控制面只在宿主侧**：容器内不挂载 `/var/run/docker.sock`，也不提供
   `docker` 可执行文件（12 号探针已实测确认）；Agent 与不可信代码都拿不到容器控制能力。
2. **无网络**：`--network none`；容器不能出网，也不能监听后被外部访问。
3. **最小权限**：`--cap-drop ALL`、`no-new-privileges`，以非 root 用户运行（需与挂载目录
   属主匹配，属实现期细节）。
4. **资源限制**：沿用已验收的 `2 CPU / 4 GiB / swap=内存 / PIDs 512`。
5. **输入只读 + 输出隔离**：见 2.3。
6. **标签化可审计**：每个容器带 `task_id` / `tool_call_id`，孤儿可被精确回收。
7. **不复用容器**：正常结束也删除；不存在"上一次调用的残留进程影响下一次"的路径。

### 2.7 孤儿容器管理

- 客户端崩溃时容器会残留（12 号探针已确认这是真实风险）；
- `OrphanSweeper` 负责兜底：启动时 + 周期扫描 `label=doc-agent.managed=1`，
  对超过 TTL（建议 `2 × sandbox_max_timeout`）或所属任务已非 RUNNING 的容器执行
  `docker rm -f`；
- 探针实测：标签扫描清理 2 个容器的总耗时为 1.25s。

---

## 3. 验收标准（实现分支必须满足）

### 3.1 离线单测（fake docker runner，不需要 Docker）

- supervisor：创建/执行/强杀/验证消失的调用序列与失败分支；
- call workspace：只读输入、产物合并、失败丢弃；
- exec wrapper：AST 末行回显（含 `None` 不打印）、异常 → 退出码 1、Agg 注入；
- 错误映射：ok / error / timeout / uncertain 四条路径；
- 孤儿扫描：TTL 与任务状态判定。

### 3.2 真机烟测（必须真实 Docker）

1. 四种进程形态（主进程 / 普通子进程 / 后台 `&` / `start_new_session`）在调用结束或超时后
   全部消失——直接复用 12 号探针的判定；
2. 超时后挂载目录**没有**延迟副作用文件；
3. 正常结束后容器不存在；
4. 并发多次调用互不影响；
5. `--network none` 下解析类任务仍可执行（PyMuPDF / openpyxl / python-pptx / docx2txt 可用）；
6. 输出限长与 P0-3 大结果卸载、`read_tool_output` 回读仍然工作；
7. 孤儿扫描器能在 TTL 内清理被遗弃的容器。

### 3.3 全量回归

`python -m pytest -q` 全部通过（当前基线 `364 passed, 1 skipped`），
并重新执行 Harness V1 八条端到端场景。

---

## 4. 待决问题（实现前需确认）

| # | 问题 | 建议 |
| --- | --- | --- |
| 1 | 是否保留旧 HTTP 沙箱后端作为降级路径？ | 建议在迁移期保留双后端（配置开关），全部验收通过后再移除 |
| 2 | 镜像是否继续使用 AIO Sandbox 镜像？ | 建议继续：解析库已在镜像内；但可跳过其 server 启动（`--entrypoint` 覆盖） |
| 3 | 容器内运行用户与挂载目录属主如何匹配？ | 实现期确定，倾向非 root + 目录属主对齐 |
| 4 | 是否需要 `--read-only` 根文件系统 + `tmpfs /tmp`？ | 作为加固项评估，注意第三方库可能写临时目录 |
| 5 | 冷启动 0.78s 是否为缓存命中值 | 需在实现分支重测（含首次拉取场景） |

---

## 5. 下一步

设计评审通过后建立独立实现分支（建议
`feat/one-shot-execution`），按 3.1 → 3.2 → 3.3 的顺序推进；在此之前不修改生产代码，
P0-5 保持未完成。
