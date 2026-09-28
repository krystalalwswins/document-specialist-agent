# 真实 LLM 端到端验收记录（进行中，未通过）

> 记录日期：2026-09-28
> 分支：`feat/one-shot-execution`
> 脚本：[`demo/real_llm_e2e.py`](../../demo/real_llm_e2e.py)
> 状态：**未通过**。链路本身已跑通，但任务未能以最终回答收口

---

## 1. 模型接口确认（通过）

```text
$ .venv\Scripts\python.exe -m demo.llm_interface_check
base_url    : https://api.deepseek.com
model       : deepseek-chat
api_key set : True (value never printed)
elapsed_s   : 2.52
reply       : 'PONG'
usage       : {'prompt': 10, 'completion': 2, 'total': 12}
attempts    : 1
```

密钥仅以布尔值出现，全文未输出任何密钥内容。

---

## 2. 端到端任务：三次运行

任务：上传 `sales.csv` → 解析 → 按 region 汇总 → 写 `reports/region_summary.csv` →
保存产物 → 给出总收入与小计。`require_artifact=true`。

| 运行 | 结果 | 耗时 | Token（prompt/completion/total） | 失败原因 |
| --- | --- | --- | --- | --- |
| #1 | FAILED | 21.5s | 25377 / 1903 / 27280 | `exceeded 8 tool-calling iterations` |
| #2 | FAILED | 26.5s | 27777 / 2175 / 29952 | `exceeded 8 tool-calling iterations` |
| #3 | FAILED | 37.0s | 44440 / 2282 / 46722 | `exceeded 12 tool-calling iterations` |

模型固定为 `deepseek-chat`；三次都是真实调用（无 fake LLM）。

---

## 3. 每次失败暴露的真实问题（均已在本分支修复）

### #1：`out/` 路径在调用之间漂移

模型写 `out/agg.json`，提交后文件落到**任务根目录**；下一次调用再读 `out/agg.json`
就 `FileNotFoundError`，模型反复重试直到耗尽迭代。

修复：`out/` 改为**任务内持久**的产物目录——每次调用把它预置进调用级私有目录，
成功后按"允许覆盖"提交回 `<task>/out`，失败则整批丢弃。路径从此稳定。

### #2：只读目录缺提示 + 迭代预算过紧

模型把文件写到任务根目录，得到裸的 `OSError: Read-only file system`，试错两轮；
8 轮预算不够。

修复：包装脚本在 `EROFS` 时追加明确提示（"产物写到 out/"）；迭代上限提升为
可配置的 `EXECUTOR_MAX_ITERATIONS`（默认 12）。

### #3：提交协议拒绝了合法的嵌套产物

用户要求 `reports/region_summary.csv`，模型写了 `out/reports/...`，被提交协议
（默认禁止嵌套）整批拒绝，且被判定为终止性错误。

修复：新增 `SANDBOX_COMMIT_ALLOW_NESTED`（默认 true，深度仍在 CallWorkspace 内受限）；
并把"提交前校验拒绝"从终止性错误改为**可重试的普通工具错误**（因为此时没有任何文件
落盘，模型换一个路径即可）。

---

## 4. 第 3 次运行的实际链路（关键证据）

第 3 次运行虽然整体 FAILED，但**业务链路已经完整走通**：

| 步骤 | 工具 | 结果 |
| --- | --- | --- |
| 0 | `read_file` | 成功读回 CSV 全部内容 |
| 1-2 | `run_python` | 成功解析并求和，得到 `north 1500 / south 950 / east 450 / total 2900` |
| 3 | `run_python` | 写根目录被拒（只读），包装脚本给出 out/ 提示 |
| 4 | `run_python` | 改为写 `out/`，成功生成 4 行 CSV |
| 5 | **`save_report`** | **成功上传产物**，返回真实预签名 URL：`http://localhost:9000/doc-agent-storage/reports/<task_id>/region_summary.csv?...` |
| 6 | `run_python` | 回读校验，`match True`，`total 2900` |

`plan_events` 显示 s1–s5 全部 `plan_step_completed`，s6 已绑定但**始终没有完成事件**，
随后模型在 12 轮迭代内未能产出最终回答。

---

## 5. Trace 定位（逐轮消息）

为排除"模型空转"的猜测，新增 [`demo/trace_executor_loop.py`](../../demo/trace_executor_loop.py)
包装 Executor 的 LLM 客户端，逐轮记录 `finish_reason / content / tool_calls`。

结果**推翻了空转假设**：12 轮**每一轮都有真实工具调用**，第 12 轮返回
`finish_reason=stop`、无工具调用、带完整最终文本，主循环据此正常收尾：

```text
round 1  tool_calls  run_python + parse_document
round 2  tool_calls  complete_plan_step(s1)
round 3  tool_calls  run_python（汇总计算）
round 4  tool_calls  complete_plan_step(s2)
round 5  tool_calls  run_python（写 out/reports/... 前的准备）
round 6  tool_calls  run_python（写入 out/reports/region_summary.csv）
round 7  tool_calls  complete_plan_step(s4)
round 8  tool_calls  save_report(out/reports/region_summary.csv)
round 9  tool_calls  read_file（回读校验）
round 10 tool_calls  complete_plan_step
round 11 tool_calls  complete_plan_step
round 12 finish=stop  最终回答，无工具调用
```

原因不是循环缺陷，而是**计划有 6 个步骤、每个 `complete_plan_step` 也占一轮**，
8 轮预算本身不够。前两次更差的失败另有原因（写只读根目录被拒、产物被协议拒绝），
那些已在前文修复。`EXECUTOR_MAX_ITERATIONS` 因此不是"掩盖空转"，而是恢复合理预算。

## 6. 真实端到端通过

同一次 trace 运行的任务结果：

```text
status      : SUCCESS
answer      : 677 字符（总收入 2900，north 1500 / south 950 / east 450）
artifacts   : reports/<task_id>/region_summary.csv (49 bytes)
plan_events : ... plan_step_completed, plan_step_completed, plan_finished
tokens      : prompt=54184 completion=3285 total=57469 cache=10880
attempts    : 14（12 轮执行 + 2 次规划/其他）
duration_ms : 20033
```

即：**文档解析、代码执行、产物提交与上传、最终回答、`require_artifact` 校验全部完成**。

## 7. 官方成功记录（本轮交付）

`demo/real_llm_e2e.py` 一次运行（真实 DeepSeek + 一次性容器 + MinIO）：

```text
被测提交   : feat/one-shot-execution @ 本轮提交
model      : deepseek-chat
status     : SUCCESS
elapsed_s  : 31.7
tokens     : prompt=47154 completion=2949 total=50103
phases     : plan=1172, execute=47126, memory_capture=1805
artifact   : reports/<task_id>/region_summary.csv (49 bytes)
answer     : 归档完成，总收入 2900（east 450 / north 1500 / south 950）+ 下载链接
decision   : Full chain held: parse + code + artifact commit + final answer.
```

`require_artifact=true` 下的产物校验通过（`ArtifactValidator` 在 SUCCESS 前复核了 OSS 对象）。

## 8. 其它本轮完成的补齐

| 项 | 结果 |
| --- | --- |
| 迭代预算改为按计划动态分配 | `min(绝对上限, 2 × 初始步骤数 + 4)`，控制调用照常计数，重规划不重置；绝对上限可配置（默认 16） |
| 超时回归（三段） | 新增断言：触发=用户超时+1s 的宿主计时（**不再继承 10s HTTP grace**）、超时后强制销毁、确认消失；并保留"超时期间与之后均无延迟副作用"的真机探针 |
| `out/` 独立副本回归 | 新增断言：每次调用拿到已提交产物的私有副本；**确认容器消失后才提交**；失败调用不修改上一轮已提交产物；未确认消失则不提交 |
| 禁止自动重放 | 断言 `SandboxTool.retry_safe is False`；提交前拒绝改为可重试的普通错误（模型可自行修正），但运行时不会自动重放原代码 |
| Linux 符号链接用例 | 在 `python:3.12-slim` 容器中实际执行 `tests/test_call_workspace.py`：**13 passed**（Windows 上为 12 passed + 1 skipped） |
| 规划健壮性 | 真实运行暴露：模型给步骤多加 `depends_on_note` 导致整单失败。改为忽略未知字段，保留必填/类型/依赖图校验 |

## 9. 结论与待办

- 真实 LLM + 一次性容器 + 文档解析 + 代码执行 + 产物提交（含 OSS 上传）**已实际打通**；
- 真实 DeepSeek 任务已达到 `SUCCESS`，`require_artifact` 校验通过；
- 仍需在收口前完成：把本轮的设计调整（`out/` 持久化 + 允许覆盖 + 销毁后提交、
  迭代预算策略）**回写 design 14**，并复跑 5.2 真机烟测确认新预算下无回归；
- P0-5 保持未完成，分支不合并。
