# Jupyter 依赖审计：一次性容器迁移的前置清单

> 审计日期：2026-09-28
> 审计分支：`main`
> 审计方式：**只读**（`rg` + 逐文件阅读），未修改任何生产代码
> 上游结论：一次性容器探针通过全部安全门槛，见
> [`12_one_shot_container_probe.md`](12_one_shot_container_probe.md)

本页回答一个问题：把执行后端从"常驻沙箱 + Jupyter"换成"一次性容器"时，
`document_tool`、`sandbox/hooks`、Jupyter 富输出三者会造成多少语义损失。

---

## 1. 审计范围与方法

```text
rg -n "jupyter|Jupyter" .
rg -n "display_data|execute_result|output_type|rich" .
rg -n "create_session|delete_session|session_id" .
rg -n "\.outputs|\.stdout|\.stderr" .
rg -n "execute_python|execute_in_sandbox|SandboxClient" .
```

再逐文件阅读 `sandbox/client.py`、`sandbox/hooks.py`、`tools/sandbox_tool.py`、
`tools/document_tool.py`、`tools/file_tool.py`、`tools/report_tool.py`、
`agent/wiring.py`、`agent/executor.py`（System Prompt）与相关测试。

---

## 2. 依赖面总览

沙箱 SDK 的三类接口在本项目的使用情况完全不同：

| SDK 接口 | 使用位置 | 是否依赖 Jupyter |
| --- | --- | --- |
| **File API**（`write_file` / `read_file` / `download_file`） | `SandboxClient` 的文件读写、`InputStager`、`ReportTool`、`HermesHookEngine` | 否 |
| **Shell API**（`exec_command`） | 仅用于路径守卫 `_guard_path`、`rm -f`、`mkdir -p` | 否 |
| **Jupyter API**（`create_session` / `execute_code` / `delete_session`） | **只有 `SandboxClient.execute_python` 一处** | 是 |

也就是说，**Jupyter 依赖被完全收敛在一个方法里**：`sandbox/client.py::execute_python`。

### 2.1 `execute_python` 的三个调用点

| 调用点 | 用途 | 读取的字段 |
| --- | --- | --- |
| `tools/sandbox_tool.py::SandboxTool.execute`（`run_python`） | 模型提交的任意代码 | `execution_to_tool_result(result)` → `result.text` |
| `tools/document_tool.py::ParseDocumentTool.execute`（`parse_document`） | 运行固定解析脚本 | `result.status`、`result.text`（取最后一行 JSON） |
| `sandbox/hooks.py::HermesHookEngine.execute_in_sandbox` | 通用执行入口 | `result.text` |

三个调用点**都只读 `result.text`**，没有一个直接读 `result.outputs`。

---

## 3. 富输出的真实消费情况

| 字段 | 生产消费者 | 结论 |
| --- | --- | --- |
| `ExecutionResult.outputs`（`execute_result` / `display_data`） | **无**（仅 `client.py` 内部拼进 `.text`） | 富输出只通过 `.text` 影响模型 |
| `ExecutionResult.stdout` / `.stderr` | **无**（仅 `client.py` 内部拼进 `.text`） | 分离本身对模型不可见 |
| `ExecutionResult.traceback` | 无 | 仅拼进 `.text` |
| `ExecutionResult.text` | 上述三个调用点 | **唯一的真实契约** |

`sandbox/client.py::_normalize` 对富输出的处理是：
`execute_result` / `display_data` → 取 `data["text/plain"]`，若不存在则 `str(data)`。

这里存在一个**既有缺陷**：图片类输出（`{"image/png": "..."}`）没有 `text/plain`，
会被 `str(data)` 变成超长 base64 字符串塞进 `.text`。虽然 P0-3 的大结果卸载会在超限时
把它落盘，但语义上仍然是错的——富二进制输出既不该进上下文，也不该被当成文本。

---

## 4. 三类清单

### 4.1 必须保留（迁移后语义必须等价）

| 能力 | 当前实现位置 | 迁移要求 |
| --- | --- | --- |
| 执行任意 Python 并拿到文本结果 | `execute_python` → `.text` | 新后端必须返回等价文本 |
| 失败可分类 | `status` → `ErrorType.TIMEOUT` / `ErrorType.EXECUTION`（`tools/sandbox_tool.py`） | 用退出码 + 包装脚本重建分类 |
| 不确定即终止、禁止重放 | `execution_uncertain` → `terminal=True` | 容器销毁天然是 uncertain，必须显式映射为终止性错误 |
| 每次执行相互隔离 | 每次新建 session，拒绝 caller-owned session | 一次性容器更强，天然满足 |
| `cwd` 落在任务目录 | `execute_python(cwd=...)` + `workspace_path` | 挂载目录 + 容器内路径映射 |
| 执行结果与文件系统协同 | 脚本写文件后由 File API 回读 | 挂载目录持久化，容器销毁不丢产物 |
| 路径守卫 | `_guard_path`（Shell API，与 Jupyter 无关） | 原样保留 |

### 4.2 可以降级（语义变化可接受，需记录）

| 能力 | 变化 | 影响评估 |
| --- | --- | --- |
| `stdout` / `stderr` 分离 | Shell 的 `output` 是合并流 | 模型看到的本来就是合并后的 `.text`，仅 `[stderr]` 前缀这一处细微差别 |
| 结构化 `error` / `traceback` | 需由包装脚本捕获异常并打印 | 可用固定 sentinel 近似还原 |
| `status` 三态归一化 | 改为退出码映射 | `0 → ok`，非 0 → error；超时由容器销毁路径表达 |
| 每轮 session 的创建/删除事务 | 由容器生命周期取代 | `tests/test_security.py` 中"created == deleted"的断言需重写 |
| `kernel_name` | 代码未使用 | 直接删除 |

### 4.3 会丢失（不做额外补偿就会改变行为）

| 能力 | 说明 | 补偿方案 |
| --- | --- | --- |
| `execute_result`：末行表达式自动显示 | 模型若写 `df.head()` 而不 `print(...)`，原后端能看到结果，新后端**什么都看不到** | ① 包装脚本用 `ast` 自动打印末行表达式；或 ② 在工具描述与 System Prompt 中强制要求 `print` |
| `display_data`：matplotlib / HTML / 富展示 | 没有 display hook，`plt.show()` 无输出 | 改为"图必须落盘 + `save_report`"，顺带修掉第 3 节的 base64 缺陷 |
| Jupyter 内核中断语义 | 本来就未生效（见 [`10_hard_timeout_probe.md`](10_hard_timeout_probe.md)） | 无损失，由容器销毁取代 |
| 内核级持久变量 | 本来就不使用（每轮新 session） | 无损失 |

---

## 5. 迁移必须连带修改的契约

1. **工具描述**：`tools/sandbox_tool.py` 的 description 写作
   "return stdout/result/error"，其中 "result" 暗示 `execute_result`。迁移后应明确
   "只有 `print()` 的输出会返回"，或实现末行表达式自动打印。
2. **System Prompt**：`agent/executor.py::_system_prompt` 目前**没有**要求模型必须
   `print`，也没有提到 Jupyter/REPL。这是最容易被忽略的一处——迁移后模型依赖末行
   表达式的既有习惯会静默失效。
3. **错误映射**：新增"容器被销毁"到 `execution_uncertain=True` / `terminal=True` 的显式映射。
4. **测试**：`tests/test_sandbox_client.py`（四类输出归一化、timeout 透传）、
   `tests/test_security.py`（session 创建/删除配对、超时终止、清理失败）需按新后端重写。
5. **`document_tool`**：`DOCUMENT_SCRIPT` 只用 `print(json.dumps(...))` + 写文件，
   **迁移友好**；但必须保证新后端捕获 stdout（含 `flush=True`），且解析脚本依赖的库
   （PyMuPDF / openpyxl / python-pptx / docx2txt）仍随镜像提供。
6. **`hooks.execute_in_sandbox`**：只返回 `.text`，迁移友好。

---

## 6. 与一次性容器的契合度

免费获得：进程级强隔离、无跨调用状态、超时强杀、零延迟副作用（见 12 号探针）。

需要新建（宿主侧、可信组件）：

- 容器生命周期管理：创建 → 就绪探测 → 挂载任务目录 → 执行 → 销毁；
- 标签化孤儿扫除器（探针实测 1.25s 可清干净）；
- 就绪探测与冷启动预算（缓存命中实测 0.78s）；
- 销毁路径到 `execution_uncertain` 的错误映射。

需要显式决策的两个设计点：

| 决策点 | 选项 A | 选项 B | 影响 |
| --- | --- | --- | --- |
| 容器粒度 | 每任务一个容器 | 每次工具调用一个容器 | 冷启动 0.78s；任务级更省，但超时边界退化为"任务级销毁" |
| 网络 | `bridge`（出网可达，可用发布端口访问 API） | `--network none`（隔离更强，但宿主无法通过发布端口访问容器 API） | 直接决定执行通道是"HTTP API"还是"宿主侧 `docker exec`" |

---

## 7. 结论

1. **Jupyter 依赖面比预期小得多**：全项目只有 `SandboxClient.execute_python` 一个方法
   依赖 Jupyter API；路径守卫与文件读写本来就走 Shell / File 接口。
2. **富输出实际上是"零消费"**：没有任何生产代码读取 `outputs` / `stdout` / `stderr`，
   唯一的真实契约是 `ExecutionResult.text`。
3. **真正的迁移风险集中在两处语义**：
   - 末行表达式自动显示（`execute_result`）会丢失，且当前 System Prompt 未要求 `print`，
     属于**静默失效**风险，必须优先处理；
   - `display_data` 本来就没有被正确支持（会被 `str(data)` 成 base64），
     迁移正好是修正它的机会。
4. **三个调用点里两个（`document_tool`、`hooks`）迁移友好**，只需保证 stdout 捕获与
   镜像内的解析库。
5. 迁移成本因此主要不在"Jupyter 富输出"，而在**容器生命周期管理**与**错误/超时语义映射**；
   具体成本核算与生产链路设计仍待下一步。

本轮为只读审计：未修改 `SandboxClient.execute_python` 或任何生产代码，
迁移成本核算与生产执行链路设计见
[`../design/14_one_shot_execution_migration.md`](../design/14_one_shot_execution_migration.md)。
P0-5 继续保持未完成。
