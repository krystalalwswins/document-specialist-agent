# Design Note 04：Agent 编排层（agent/ + tools 抽象）

## 1. 本次修改解决的工程问题

- **单轮脚本 → 多轮 Agent**：原 `agent_core.py` 只调一次 LLM、执行一次工具就结束，工具结果从不回传。`Executor.run` 实现真正的 tool-calling 循环，直到 LLM 给出最终回答或超上限。
- **工具硬编码 → 动态注册**：原来工具 schema 和分发写死在 `main()`。`BaseTool` + `ToolRegistry` 让工具可注册、可替换，schema 由工具自己声明。
- **计划缺失 → Planner 结构化分解**：LLM 通过 `create_plan` 函数调用产出有序步骤，作为执行上下文。
- **任务与编排脱节 → Orchestrator 串起生命周期**：创建/启动/成功/失败全程落在 `Task` 状态机上，失败可追踪。

## 2. 完整调用链

```text
User -> AgentOrchestrator.run(user_input)
  -> TaskManager.create_task (CREATED) -> start_task (RUNNING)
  -> Planner.plan -> LLM(create_plan 函数调用) -> Plan(step[])
  -> Executor.run
       loop(<= max_iterations):
         LLM.chat(messages, tools=ToolRegistry.to_openai_tools())
         if no tool_calls -> return final answer
         for each tool_call:
           TaskManager.add_step/start_step
           ToolRegistry.execute(name,args) -> ToolResult
           TaskManager.succeed_step | fail_step
           append tool message -> loop
  -> TaskManager.succeed_task({"answer": ...})
  （异常 -> fail_task 后 re-raise）
```

## 3. 最关键的代码位置及解释

- `agent/executor.py:31` `Executor.run` —— 整个项目最核心的一处：多轮工具调用循环。
- `agent/executor.py:76` `_assistant_message` —— 把 pydantic 消息对象转成可回传的 dict，兼容真实 SDK 与测试 fake。
- `agent/executor.py:45` 附近 —— 每个工具调用都落成一步，成功/失败都记录，是可观测性来源。
- `agent/planner.py:67` `Planner.plan` —— 用 `tool_choice` 强制 LLM 返回 `create_plan`，避免自由文本。
- `agent/planner.py:36` `CREATE_PLAN_TOOL` —— 计划的结构化契约。
- `agent/orchestrator.py:26` `Orchestrator.run` —— 生命周期编排 + 失败回滚。
- `agent/orchestrator.py:35` 失败分支 —— 只在非终态才 `fail_task`，避免覆盖已 SUCCESS 的任务。
- `agent/llm_client.py:32` `LLMClient.chat` —— 统一 model/tools/tool_choice，可注入 fake。
- `tools/base_tool.py:27` `BaseTool` —— 工具抽象，schema 与执行分离。
- `tools/base_tool.py:41` `to_openai_schema` —— 动态生成 Function Calling schema。
- `tools/tool_registry.py:10` `ToolRegistry` —— 注册/查询/批量 schema/分发。
- `tools/tool_registry.py:29` `execute` —— 按名字分发到具体工具并返回 `ToolResult`。

## 4. 必须掌握的知识点

- OpenAI Function Calling：`tools` + `tool_choice` + `tool_calls` + `role:"tool"` 消息的闭环。
- 工具调用的多轮循环与终止条件（无 `tool_calls` / `max_iterations`）。
- 依赖注入：Planner/Executor/Orchestrator 都注入依赖，单测用 fake。
- 协议式解耦：Executor 只依赖 `ToolRegistry` 接口，不依赖具体工具。
- 消息历史管理：按序追加 assistant 消息与 tool 结果，是 LLM 能"看到执行结果"的关键。

## 5. 最容易让我误解的地方

- 不是"调一次工具就停"：真正的 Agent 是"LLM 决定调工具 → 执行 → 把结果塞回消息 → 再让 LLM 决定"，直到它不再返回 `tool_calls`。
- `message.content` 在有 tool_calls 时常常是 `None`：最终答案只出现在"没有 tool_calls 的那一轮"。
- plan 步骤 ≠ task 步骤：当前 MVP 里 `Plan` 只作为 LLM 上下文，`task.steps` 记录的是实际工具执行轨迹（刻意简化，见第 8 节）。
- `role:"tool"` 消息必须带 `tool_call_id`，否则 API 报错；这是循环里最容易漏的字段。
- 工具返回失败不一定要中断整个任务：`Executor` 把失败记录为 FAILED 步骤并继续，让 LLM 决定是否重试/换路。

## 6. 我应该主动回答的 3~5 个问题

- 为什么叫 Agent 而不只是"调 LLM 的脚本"？→ 多轮 tool-calling 闭环 + 计划 + 任务状态 + 可观测步骤。
- 工具为什么抽象成 `BaseTool`？→ 动态注册、schema 由工具自述、执行与声明分离、可替换。
- 怎么防止 LLM 无限循环调工具？→ `max_iterations` 上限，超限抛 `MaxIterationsError`。
- 失败怎么处理？→ 单步失败记为 FAILED 继续；任务级失败 `fail_task` 后向上抛，状态机保证不被二次覆盖。
- 为什么 LLM 要注入？→ 单测用脚本化 fake，不依赖真实 API key。

## 7. 如何测试本模块

```powershell
.venv\Scripts\python.exe -m pytest tests/test_tool_registry.py tests/test_planner.py tests/test_executor.py tests/test_orchestrator.py -q
```

全离线：fake LLM（脚本化返回）+ fake 工具。覆盖：工具注册/schema/分发、计划解析与空计划、工具调用循环、失败步骤记录、超限、Orchestrator 成功/失败生命周期。

## 8. 面试官最可能追问的 5 个问题

- plan 步骤和 task 步骤什么关系？→ MVP 里 plan 是执行上下文、task.steps 是执行轨迹；后续可加 plan_id 做映射/对齐。
- 工具结果太大怎么办？→ 截断 + 摘要后再回传；或让工具返回引用（如 OSS key）而非全文。
- 怎么并行调用多个工具？→ 一轮 `tool_calls` 里多个调用可顺序执行（MVP）；并行需并发执行 + 结果按 id 对齐。
- token 消耗怎么统计？→ `response.usage` 累加进 `task.metrics`，Phase 2 评估用（本步未做）。
- 死循环怎么根治？→ 除 max_iterations 外，可加"相同调用去重/衰减"或让 LLM 输出显式 FINISH 信号。

## 9. 项目核心代码（必须真正理解）

- `Executor.run` 的循环（`agent/executor.py:31`）。
- `_assistant_message` 消息回传（`agent/executor.py:76`）。
- `Planner.plan` 的 `tool_choice` 强制（`agent/planner.py:67`）。
- `Orchestrator.run` 生命周期 + 失败回滚（`agent/orchestrator.py:26`）。
- `BaseTool` / `ToolRegistry`（`tools/`）。

## 10. 框架/基础设施细节（暂可不深入）

- `LLMClient` 的 OpenAI SDK 封装细节（薄适配层，会用即可）。
- openai 3.x 的 pydantic 响应对象内部结构。
- 具体工具（sandbox/file/report）尚未实现，属下一步，不在本模块范围。
