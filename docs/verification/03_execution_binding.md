# P0-2：执行步骤绑定与局部重规划

日期：2026-09-18。开发基线：`7d97c34f5febda1b9a42fef2e127349f353f2160`（P0-1 合入后的 `main`）。
本轮仅完成 `task_points.md` 的 P0-2，不改动 P0-3/P0-4 及之后编号。

## 解决的问题

P0-1 之后计划已经能落盘，但 Executor 仍只把计划当提示词：工具调用记录没有
`plan_step_id`，依赖不构成门禁，“工具返回 success”就等于步骤完成，也没有任何
局部重规划能力和预算。本轮把计划变成真正的运行时调度契约。

## 调用链与职责

1. `Orchestrator.run_task` → `Planner.plan` → `TaskManager.set_plan`
   在计划落盘的同时追加 `plan_created` 事件（版本由 Runtime 决定）。
2. `Executor.run` 构造 `PlanRunState(task.plan, task.plan_events)`：
   步骤状态全部由事件派生，不额外维护一份可变状态。
3. 每轮把 `_system_prompt(state)` 写回 system 消息（不追加新消息），
   模型因此总能看到 completed / running / ready / blocked 与计划版本。
4. 发给模型的工具 schema = 注册表可见工具（每个都注入必填 `plan_step_id`）
   + 两个运行时控制调用 `complete_plan_step`、`request_replan`。
5. 每个 tool call 的顺序是：
   剥离 `plan_step_id` → `_bind_step` 门禁 → `TaskManager.add_step(plan_step_id,
   tool_call_id)` → `start_step` → `ToolRegistry.execute` → `succeed_step|fail_step`
   → 追加 `role=tool` 观察；绑定结果写 `plan_step_bound`（或
   `plan_unbound_tool_call`、`plan_binding_rejected`）。
6. `complete_plan_step(step_id, evidence)` 是唯一的“步骤完成”入口：校验步骤存在、
   依赖已完成、未完成过、证据非空，然后写 `plan_step_completed`（附
   `completion_criteria` 与证据）；工具 success 不会自动完成步骤。
7. `request_replan(reason_code, reason)` 校验原因码、剩余未完成步骤、
   是否配置 Planner 与 `max_replans` 预算；预算内调用
   `Planner.replan(目标, 当前计划, 已完成步骤+证据, 失败观察, 可用工具)`，
   经 `TaskManager.replace_plan` 应用版本 +1 的新计划；超预算抛
   `ReplanBudgetExceededError`，任务由 Orchestrator 明确判失败。
8. 工具失败只记 `plan_step_failed` 并回注观察，绝不自动重规划。
9. `Orchestrator._execute_with_recovery` 每轮重新读取当前计划，
   避免产物修复重跑时用旧版本覆盖上一轮的重规划结果。

## 计划事件（`Task.plan_events`，append-only）

| kind | 写入者 | 关键字段 |
| --- | --- | --- |
| `plan_created` | `Task.set_plan` | `version`, `step_ids` |
| `plan_step_bound` | Executor | `step_id`, `tool`, `tool_call_id`, `task_step_id`, `version`, `implicit` |
| `plan_unbound_tool_call` | Executor | `tool`, `tool_call_id`, `candidates`, `version` |
| `plan_binding_rejected` | Executor | `step_id`, `tool`, `violation`, `version` |
| `plan_step_failed` | Executor | `step_id`, `error`, `error_type`, `tool_call_id` |
| `plan_step_completed` | Executor | `step_id`, `evidence`, `completion_criteria`, `version` |
| `plan_completion_rejected` | Executor | `step_id`, `violation`, `version` |
| `plan_replan_requested` | Executor | `reason_code`, `reason`, `remaining_step_ids`, `version` |
| `plan_replanned` | `Task.apply_replan` | `from_version`, `to_version`, `reason_code`, `reason`, `preserved_step_ids` |
| `plan_replan_rejected` | Executor | `reason_code`, `violation` |
| `plan_replan_exhausted` | Executor | `used`, `max_replans`, `reason_code` |
| `plan_finished` | Executor | `satisfied`, `remaining_step_ids`, `version` |

## 调度与判定规则

- 可调度 = 依赖全部 SUCCESS 且自身未 SUCCESS：`PENDING`/`FAILED` 进入 `ready`，
  `RUNNING` 可以继续接收同一计划的更多工具调用。
- 声明了 `plan_step_id` 的调用必须满足：步骤存在于当前版本、依赖已完成、未完成过；
  违规时**不执行**工具、不新增 `TaskStep`，只回注观察并记事件。
- 未声明 `plan_step_id` 时，只有运行时能唯一定位（唯一 RUNNING，否则唯一 ready）
  才隐式绑定（事件 `implicit = true`）；无法唯一归属时按未绑定执行并记录
  `plan_unbound_tool_call`，保持降级路径可观测而不是静默。
- 已完成步骤不可重放，也不会进入重规划范围：`PlanRunState` 的派生状态优先，
  `Task.apply_replan` 额外要求已完成步骤在版本 +1 中逐字段不变。
- `max_replans` 是 Executor 构造参数（默认 2，装配点可覆盖），超限即整任务终止。

## 边界与兼容

- `TaskStep` 新增 `plan_step_id`/`tool_call_id`，`Task` 新增 `plan_events`，
  都是带默认值的增量字段；旧 JSON 缺这两个键时读取为 `None`/`[]`。
- 控制调用在 Executor 内拦截，不进入 `ToolRegistry`：工具白名单、Schema 校验、
  权限、任务命名空间与 `security_events` 行为完全不变。
- `plan_step_id` 注入的是 schema 副本，调用前剥离，所以工具自身的
  `additionalProperties: false` 仍然有效（有测试用严格 schema 证明）。
- `Executor.run` 签名不变；`planner`/`max_replans` 均为新增可选构造参数，
  未注入 Planner 时重规划请求只变成观察加 `plan_replan_rejected` 事件。
- 计划全部完成**不是**任务成功的门槛：产物校验仍是成功判据；
  模型提前收尾时只记录 `plan_finished.satisfied = false` 与剩余步骤。
- 与注册工具同名的 `complete_plan_step`/`request_replan` 调用会被控制平面拦截；
  本轮不为此增加冲突检测。

## 实际验证

环境：Windows 11、Python 3.13.2（工作区 `.venv`）、pytest 9.1.1。
未修改依赖文件，未调用真实 LLM、Docker 或 MinIO。

| 验证 | 结果 |
| --- | --- |
| 新验收用例在改动前运行 | 2 个模块导入失败 + 14 项失败，确认能捕获缺失能力 |
| 本任务相关测试（含新用例，13 个文件） | 214 passed，1 skipped |
| 完整 `python -m pytest -q` | **323 passed，1 skipped**（基线 275 passed / 1 skipped） |
| `git diff --check` | 通过 |

实际命令（工作区虚拟环境）：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

skip 项是 Windows 无法创建符号链接的远程路径守卫测试，与 P0-2 无关；
唯一警告来自 Starlette/AnyIO 的弃用别名。新增用例覆盖：事件派生状态与依赖门禁、
已完成步骤不可重放、失败步骤仍可换路、工具 success 不等于步骤完成、
证据缺失/依赖未满足/未知步骤被拒、控制调用不产生 `TaskStep`、
隐式绑定与无法归属的调用、工具错误只回注观察、重规划保留已完成步骤并替换未完成部分、
重规划输入包含目标/计划/已完成/失败观察/可用工具、原因码校验、未配置 Planner、
重规划预算终止并让任务明确失败、重规划结果落盘且重启后经 API 可查、
旧任务 JSON 兼容、重规划后残留步骤引用被拒、产物修复重跑使用当前计划版本。

本轮为离线工程验证：没有真实模型效果、Docker 沙箱或 MinIO 实测，
也没有实现 P0-3 的大结果卸载与 P0-4 的上下文压缩熔断。
