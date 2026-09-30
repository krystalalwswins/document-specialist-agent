# T2：每次模型调用的实际输入输出

## 目标与范围

覆盖 Planner.plan、Executor 各轮、Planner.replan、ContextCompactor、MemoryExtractor。
它们都调用同一个 LLMClient.chat，因此不复制五套采集逻辑。
新增根任务包装是模型追踪的必要依赖；工具执行、计划版本快照、输入装载等细分埋点仍在 T3。
现有 Task/metrics 继续作为状态与预算事实源；新记录不回流预算，避免重复计费。

## 调用链

AgentOrchestrator.run_task 在 worker 内创建根 Trace，绑定 ContextVar → 原任务流程 →
LLMClient.chat → GenerationCapture.start → 真正 provider 请求 → finish → 最终任务结果 → 根结束 → reset。
根结束在成功后的记忆提取之后。ContextVar 绑定在工作线程内部，不依赖提交 HTTP 请求的上下文自动跨线程传递。
每个任务 finally 恢复上下文。未来如任务内另开线程必须显式传递上下文，本轮同步模型链不需要。

## 输入快照

在每次 provider 请求之前，记录实际 kwargs：model/messages，以及实际传入的 tools/tool_choice。
没有主动发送 temperature 等参数时不伪造 provider 默认值。
实际系统消息和召回记忆已经在 messages 中；每轮是压缩处理后的真实请求。
request_hash 使用基础脱敏后的紧凑 UTF-8 JSON 字节 SHA256（保留字典插入序；不是跨序 canonical hash）。
它覆盖完整请求，不仅是模板。TRACE_PROMPT_VERSION 是人为版本标签，默认 harness-prompts-v1；
调用方修改提示词应更新标签，实际输入快照仍是最终证据。

## 输出与重试

显式保存可见 choices/message.content/refusal/tool_calls/finish_reason、响应 id 和实际模型。
只选可见字段，不保存 reasoning_content 等隐藏推理字段，也不把 API Key/base URL 鉴权参数写入请求。
每一次真实请求一个 observation_id；同次 chat 重试共享 logical_call_id，attempt 从 1 递增。
失败记录 ERROR、异常类型与基础脱敏消息，usage/cost 为未知；之后重试不会覆盖失败记录。
Generation 都先挂在根 span 下；轮次/phase 通过 metadata 关联，T3 可细化操作树。

## 阶段、用量与费用

复用现有 on_event 回调，给 UsageMeter.event_sink/TaskManager.metric_sink 附加 trace_metadata。
包括 task_id/phase/iteration（调用方提供时）。不增加 Fake LLM 的 chat 参数，不改变原事件内容。
Token 来自 provider usage；价格复用 PricingCatalog。仅 prompt/completion 明确存在且价格已知时估价。
失败/缺 usage/未知模型的费用为 null，价格版本写入，估价不等于账单。
计量只作用于 generation，不能再将根级累计数字加一次。
code_version 来自 TRACE_CODE_VERSION；未配置为 null，不运行 Git 子进程猜测。
本轮没有每版计划快照或每次 plan_version 绑定，T3 补齐。

## 配置与可查询性

TRACE_ENABLED=true 默认启用正常 wiring 的 TraceRecorder；false 时不创建任务 Trace。
TRACE_PROMPT_VERSION=harness-prompts-v1；TRACE_CODE_VERSION 可选；复用 TRACE_STORE_DIR/TRACE_INLINE_BYTES。
走真实 build_orchestrator 的任务现在有根+模型轨迹，可通过 T1 API 查询。
注入的测试 Orchestrator 默认 recorder=None，保留旧行为；脚本化 FakeCaseExecutor 不是实际 LLMClient 调用，不自动产生 generation。
新根 input 明确标 capture_scope：T2 model calls and root only，不能把 COMPLETE 理解为所有工具已经可观测。

## 失败边界

Recorder 失败不阻止模型或业务；输出序列化失败标记 INCOMPLETE，原模型响应继续返回。
Trace 创建失败时取消该任务模型采集，不绑定其他任务；finally 清理 context。
硬退出仍留下 OPEN 操作，按 T1 显式恢复机制处理。不会自动重放任务，也不从 trace 恢复模型执行。
基础脱敏不是通用 PII 检测；不能保证任意用户自由文本的秘密均被识别。

## 如何排查

从任务 trace 找 phase=execute 的第 N 轮 generation，读取 input（或 payload API）确认当轮上下文；
再查看 output 的 tool_calls.function.arguments，可看到模型请求执行的代码。
此时只能证明模型请求了这段代码，实际经过 Registry 重写的参数及执行结果必须等 T3。
最终根 output 为服务端 Task.result，不代表前端已收到或展示。
