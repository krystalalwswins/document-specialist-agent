# Retry Design Review（实现前评审，暂未写代码）

## 1. 工程目标与解决的问题

**目标**：在 Agent 执行工具时，对"临时性故障"做有界、可观测的自动重试，把偶发故障从"任务失败"变成"单次尝试失败后恢复"，同时让"确定性错误"快速、可解释地失败。

**解决的 Agent 真实问题**：

- Agent 的本质是驱动一堆外部依赖（沙箱、OSS、LLM、网络），它们天然不可靠：网络抖动、容器冷启动、429 限流、超时。
- 多步任务很脆弱：5 步里第 3 步一次网络抖动就全盘失败，重跑要重来（重复扣 token、重复执行已成功的步骤）。
- 没有重试时，"成功率"被偶发故障拉低，且无法区分"真失败"和"假失败"。

一句话：Retry 不是为了消灭错误，而是为了"从瞬态故障中恢复，并对确定性错误快速失败"。

## 2. 什么该 Retry，什么不该

核心判据：**同一个请求原样重发，有多大可能成功？** 可能成功才重试；确定性失败不重试。

| 错误类别 | 是否 Retry | 原因 |
| --- | --- | --- |
| 临时性错误（网络抖动、连接重置、429 限流、5xx、沙箱冷启动/未就绪） | ✅ 重试 | 状态是暂时的，原样重发很可能成功 |
| Timeout | ✅ 重试（有界） | 可能是冷启动/瞬时负载；但设总时长上限，避免长时间任务反复超时 |
| LLM/API 网络错误（连接失败、超时、429、5xx） | ✅ 重试 | 同上；但 401/400/404 不重试 |
| Tool 参数错误（缺字段、类型错、JSON 非法） | ❌ 不重试 | 同参数重发无意义；应由上层 LLM 修正参数，而非策略盲重试 |
| Permission Denied | ❌ 不重试 | 重试不会改变授权，必须立即失败并给出安全信号 |
| Sandbox 执行失败（代码抛异常，如 NameError、除零） | ❌ 不重试（默认） | 同一段代码原样重跑还是同样异常 |
| 业务逻辑错误（找不到文件、筛选无结果、文件名错） | ❌ 不重试 | 需要 Agent 换方案，不是"再执行一次相同调用" |

最容易被误解的一点：**"代码执行报错" ≠ "基础设施故障"。** 沙箱内核死掉/未就绪 → 可重试；代码本身 NameError → 不重试。分类器必须区分这两者。

## 3. Retry Policy 设计

不是 `try/except + for`，而是把"要不要重试、等多久、为什么"抽成**纯决策**：

```text
RetryPolicy(max_attempts, base_delay, backoff_factor, max_delay, jitter)
  .decide(attempt: int, error: ErrorType) -> RetryDecision

RetryDecision
  should_retry: bool
  delay_seconds: float
  reason: str          # 观测用，例：transient_network_error
  final: bool          # 是否已达最终失败
```

- `max_attempts`：总尝试次数（含第一次），默认 3。
- `retryable / non-retryable`：由独立的 `classify(error) -> ErrorType` 决定，Policy 只消费分类结果，不自己猜。
- `retry delay`：指数退避 + 抖动（`base * backoff^n + jitter`），避免重试风暴；`max_delay` 封顶。
- `attempt tracking`：每次决策带 attempt 序号；`final` 标记"这是最后一次，不再重试"。

退避示例（max_attempts=3, base=1s, factor=2, max=10s）：第 1 次失败后等 ~1s，第 2 次失败后等 ~2s，第 3 次失败即最终失败。

为什么这样设计：Policy 是**纯逻辑、无 IO、可穷举单测**；分类器把"什么错误可重试"集中一处，新增错误类型只改分类器；执行循环（谁 sleep + 重发）与决策解耦。

## 4. Retry 放哪一层

| 层 | 优点 | 缺点 |
| --- | --- | --- |
| Tool 内部 | 工具最懂自己的错误语义 | 每个工具重复实现、难统一观测、工具变重、测试变难 |
| **Executor 层** | **单点统一策略；天然有 task_id/tool_name/attempt 上下文；工具保持纯、无状态；观测集中** | Executor 不具体懂某工具的错误 → 用"错误分类器"补足 |
| Orchestrator 层 | 能重跑整个计划 | 粒度太粗：重试 = 重跑整个计划/重新规划，代价高，语义是"任务级重规划"而非"工具重试" |

**结论：放 Executor 层。** 理由：一次"工具调用"的发生地就是 Executor，这里正好具备观测所需的全部上下文；工具保持无状态、可测；Orchestrator 层的"任务级重试/重规划"是另一个问题，留到后面。

补充：底层 SDK（如 boto3）自带的重试属于"依赖内部重试"，与本层不冲突；本层是"跨工具统一的重试语义"。

## 5. 完整失败恢复调用链

成功恢复：

```text
User -> Orchestrator.run_task -> Executor.run（外层 tool-calling 循环）
  -> Executor._invoke_tool(tool_name, args)      # 新增：带重试
       -> RetryPolicy.decide(attempt=1) -> 执行
       -> ToolRegistry.execute(tool_name, args) -> 工具 -> FAILURE(临时性)
       -> classify(error) = TRANSIENT
       -> RetryPolicy.decide(attempt=1, error) -> should_retry=True, delay=1s
       -> sleep(1s) -> attempt=2
       -> ToolRegistry.execute(tool_name, args) -> 工具 -> SUCCESS
  -> 记录 step=SUCCESS, attempts=2, 观测事件
  -> Executor 继续 tool-calling 循环 -> 最终答案 -> succeed_task
```

最终失败：

```text
... 尝试 1/2/3 均 FAILURE（或遇到 non-retryable）
  -> RetryPolicy.decide(attempt=3, error) -> final=True, should_retry=False
  -> 记录 step=FAILED, attempts=3, error_type, error_message
  -> 把 "[tool error] ..." 回传给 LLM（沿用现有 Executor 行为）
  -> LLM 决定换方案 / 或最终任务 FAILED
```

要点：**重试是"内层循环"（针对单个工具调用），tool-calling 是"外层循环"（针对 LLM 多轮决策），两者正交。** 最终失败后仍把错误交还 LLM，让 Agent 有机会换思路，而不是直接判任务死刑。

## 6. 最小可观测性

每个工具调用产出若干"尝试事件"，最终聚合成一个 step 结果。

必记字段：

| 字段 | 来源/含义 |
| --- | --- |
| task_id | 任务 id |
| tool_name | 工具名 |
| attempt | 第几次尝试（1-based） |
| error_type | 分类器给出的类型（TRANSIENT / PARAM / PERMISSION / EXEC / TIMEOUT / ...） |
| error_message | 原始错误摘要 |
| retry_reason | 决策原因（transient_network_error / non_retryable_perm_denied / max_attempts_exhausted） |
| duration_ms | 单次尝试耗时 |
| final_status | 该工具调用最终 SUCCESS / FAILED |

落点建议：

- `TaskStep` 增加 `attempts` 字段，保持 step 语义不变。
- `task.metrics["retry_events"]` 存尝试事件列表（扁平、可查询），供 Phase 2 Evaluation 直接统计。
- 同时用 `logging` 输出同样关键字段，便于运行期排查。

## 7. 边界与关键决策（待你确认）

1. 本次只做**工具层重试**；LLM 调用重试是同一 Policy 的另一个调用点（`LLMClient`），可后接，不混入本次。
2. **前置改造**：当前 `ToolResult` 只有 `success/error` 字符串，无法区分"基础设施异常"和"逻辑失败"。实现时需给 `ToolResult` 增加 `error_type`（或让工具对基础设施异常 re-raise），否则分类器无从判断——这是实现前必须补的一块。
3. 分类器能区分"沙箱基础设施故障"（SDK 抛异常）vs"代码执行错误"（`status=error`）：前者可重试、后者不重试；实现前我会再核对 SDK 行为确认这条边界。
4. 退避参数默认：max_attempts=3、base_delay=1s、backoff=2、max_delay=10s、带抖动。

## 8. 实现记录（最终实现与设计差异）

已按评审实现，落点与设计一致，仅有一处保守取舍记录如下。

**已实现：**

- `ErrorType`（`tools/base_tool.py`）：6 个枚举值 + `retryable` 属性，含义与第 2 节一致。
- `classify_exception`（`retry/retry_policy.py`）：把抛出的异常映射为 ErrorType，供 Executor 兜底分类。
- `RetryPolicy` / `RetryDecision`（`retry/retry_policy.py`）：纯决策，指数退避 + 抖动，参数为确认的默认值。
- Executor 集成（`agent/executor.py`）：新增 `_invoke_tool` 内层重试循环；`TaskStep.attempts` 记录总尝试次数；`task.metrics["retry_events"]` 记录每次尝试的 8 个字段。
- 工具改动：`SandboxTool` 按 `status` 显式给 `TIMEOUT`/`EXECUTION`；`FileTool`/`ReportTool` 不再自己 catch，改为让异常上抛、由 Executor 统一分类。

**与设计的差异（保守取舍）：**

- `classify_exception` 对未知异常默认归为 `BUSINESS`（不可重试），避免"拿不准就重试"放大错误。
- 我们的 `SandboxError`/`StorageError` 暂未单独分类（默认落到 `BUSINESS`）。这是为了不把 retry 模块反向耦合到 sandbox/storage；后续可让这两个异常自带 `error_type`。
- 本次只实现 Tool 层重试；LLM / Task / Workflow 级重试未实现（按约定）。

**真机验证结果（`python -m demo.retry_demo`）：**

- flaky：第 1 次失败 → 重试 → 第 2 次成功（attempts=2）。
- always_fail：连续 3 次失败 → `max_attempts_exhausted` 最终失败（attempts=3）。
- invalid_arg / permission_denied：不可重试，仅 1 次（attempts=1）。
- 四种场景的 `retry_events` 均正确写入 `task.metrics`。
