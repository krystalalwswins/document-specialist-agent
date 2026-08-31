# Design Note 08：工具层重试（retry/）

## 这一步解决了什么

Agent 靠工具驱动外部依赖（沙箱、OSS、网络），这些依赖会偶发故障。之前一次网络抖动就直接判工具失败、拖垮任务。现在给"工具调用"加上有界、可观测的自动重试：临时故障重试几次恢复，确定性错误（参数错、权限、代码 bug）立刻失败、不浪费重试。

## 核心设计（三件事）

1. **错误分类** `ErrorType`：`TRANSIENT` / `TIMEOUT` 可重试；`INVALID_ARGUMENT` / `PERMISSION_DENIED` / `EXECUTION` / `BUSINESS` 不可重试。判断标准就一句：同一请求原样重发有多大可能成功。
2. **策略** `RetryPolicy`：纯决策 `decide(attempt, error_type) -> RetryDecision`。指数退避 `base * factor^(attempt-1)` + 抖动，封顶 `max_delay`。指数退避给故障恢复时间，抖动避免大量并发重试"同刻齐射"（thundering herd）。
3. **位置** Executor 层：`_invoke_tool` 内层循环只针对单次工具调用，和外面 LLM 的 tool-calling 循环正交。

## 调用链

```text
Executor.run -> _invoke_tool(name, args)
  loop:
    ToolRegistry.execute -> ToolResult / 抛异常
    成功 -> 记录 SUCCESS，返回
    失败 -> classify -> RetryPolicy.decide(attempt, error_type)
      should_retry -> sleep(delay) -> 下一次尝试
      else         -> 记录最终 FAILED，返回
  -> 写 TaskStep.attempts + task.metrics["retry_events"]
```

## 关键代码

- `tools/base_tool.py` 的 `ErrorType` —— 每个值的含义和 `retryable` 都写在枚举里，新错误类型只需加一行。
- `retry/retry_policy.py` 的 `RetryPolicy.decide` —— 全部重试语义集中在一处，可穷举单测。
- `agent/executor.py` 的 `_invoke_tool` —— 真正执行"重试循环 + 观测记录"的地方。
- `agent/executor.py` 的 `_make_event` —— 8 字段观测事件，供 Phase 2 Evaluation 直接统计。

## 如何验证

```powershell
.venv\Scripts\python.exe -m pytest tests/test_retry.py -q
.venv\Scripts\python.exe -X utf8 -m demo.retry_demo
```

单测覆盖策略/分类/四种场景；demo 用脚本化 LLM 跑真实 Executor + RetryPolicy，确定性展示失败→重试→成功、超限失败、不可重试三类路径。

## 面试要点

- 一句话：Retry 只救"瞬态故障"，不掩盖"确定性错误"。
- 追问点：指数退避 vs 固定重试、jitter 的 thundering-herd 问题、重试放哪层（Tool/Executor/Orchestrator）、如何保证观测（retry_events）。

## 最容易误解的一点

- 工具报错 ≠ 要重试。代码 `NameError`、参数错、权限拒绝这类"原样重发也一样失败"的错误必须快速失败；只有网络抖动、429、冷启动、超时这类瞬态才重试。
