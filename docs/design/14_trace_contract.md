# T0：完整执行 Trace 数据契约与学习说明

## 1. 状态与边界

基线 `4899a5c`；本编号只提供模型、记录规范和验收资产。
未接入主链，未增加存储、查询 API 或 Langfuse；不能宣称运行中的任务已经产生完整 Trace。
既有 Task/steps/metrics 保持原状。T1 实现 Recorder/存储，T2 模型埋点，T3 工具与交付，T4 导出，T5 验收。

## 2. 为什么增加独立模型

Task 是任务状态；Trace 是执行证据。现有 TaskStep 不保存工具参数，llm_events 不保存完整模型请求响应。
新增 observability/model.py，不把大消息塞进 Task，也不重写 O-P-E。
调用链（后续接入）：Orchestrator → TraceRecorder → TraceEvent → 本地存储 → Langfuse 适配器。
T0 只实现 TraceEvent、TraceManifest、Payload。

## 3. 身份与生命周期

- 一个已接受的用户任务一个 trace_id（32 位非零小写十六进制），关联已有 task_id。
- observation_id 是 16 位非零小写十六进制；parent_id 指向同 Trace 的父操作。
- 根操作覆盖后台任务生命周期，不随 POST 返回而结束。后续记忆提取若属于任务尾处理，也在根操作结束前记录。
- 根类型 span；模型为 generation；工具为 tool。装载、压缩流程、校验为 span。
- started/RUNNING 和 ended/SUCCESS|ERROR|CANCELLED|INTERRUPTED 成对记录，沿用相同 observation_id。
- occurred_at 为带时区 ISO 时间；started/ended 时间分别取对应事件时间。耗时 metadata.duration_ms 使用单调时钟差，不用墙钟推算成本。
- sequence 是每条 Trace 内递增的事件写入序号，不等于并发操作的因果顺序；因果靠 parent_id、tool_call_id 等关联。
- Recorder（T1）负责单根、父节点存在、无环、同任务关联、序号唯一、禁止重复开始/结束及结束后更新。T0 只验证单记录合法性，不能验证跨记录图。
- 程序异常退出留下未闭合操作，后续恢复可补 INTERRUPTED，不能伪造原结束时间；metadata 标记 synthetic=true、恢复时间和原因。

## 4. 字段与快照

TraceManifest：schema_version、trace_id、task_id、root_id。
TraceEvent：上述关联字段，加 sequence/type/name/event/status/occurred_at/input/output/metadata/error。
构造时生成 JSON 快照；to_dict 每次返回新对象。修改原 messages 或 metadata 不会篡改已序列化证据。
调用方和导出方必须使用 to_dict，不直接序列化 dataclass.__dict__。
Payload 三种互斥状态：

| mode | 字段 | 含义 |
| --- | --- | --- |
| inline | value | 脱敏后的 JSON；null 是合法实际值 |
| reference | ref/sha256/size_bytes | 大内容逻辑引用，不接受路径或 URL |
| unavailable | reason | 未采集、采集失败或策略省略，不能冒充空值 |

SHA256 和 size_bytes 对应脱敏后实际存储的 UTF-8 字节。完整性由 T1 读写时检查，T0 不读文件、不验证文件存在。
未来事件增加字段需要版本迁移；当前拒绝未知 schema_version。

## 5. 各类操作的输入输出约定

| 类型/位置 | input | output | metadata |
| --- | --- | --- | --- |
| 根任务 | 用户问题、输入文件声明 | 服务端最终结果、产物标识 | code_version、配置版本、session_id（可选） |
| generation | 实际 messages/tools/tool_choice/请求配置 | 可见返回消息、tool_calls、finish_reason、provider request id（若提供） | phase、iteration、plan_version、prompt_version、prompt_hash、model、attempt、logical_call_id、usage、estimated_cost_usd、pricing_version、duration_ms |
| tool | raw_arguments、effective_arguments | 结构化完整工具结果 | tool_call_id、plan_step_id、task_step_id、attempt、logical_call_id、duration_ms |
| plan 快照 | 触发原因、旧计划引用 | 新计划完整快照 | from_version、to_version |
| context | 压缩前消息引用 | 压缩后消息引用 | 移除/保留分组、预算、原因 |
| memory | 查询与作用域 | 实际召回/注入记录 | 来源 ID、状态 |
| validation | 产物标识 | 各项检查 | 校验器版本 |

工具完整结果与 model_visible_output 必须分别记录（后者可置于 output 对象中），预览不代表完整返回。
敏感数据经脱敏后才能构造 Payload；T0 模型不实施自动脱敏，T1 才提供策略。
error 使用 type/message，可选 traceback 引用；不得记录凭证、鉴权请求头和包含密钥的 URL。
Prompt hash 针对有明确定义的编码后的快照；Prompt 版本只是标签，不能代替实际消息。
未知 code_version/model/usage/cost 使用 null 或注明 unavailable，不填伪造零值。
实际模型请求配置优先于默认值，未发送的 provider 默认参数记为未知。

## 6. 重试案例：如何组织而不是覆盖

根 span R 开始 → generation A 开始/ERROR → generation B 开始/SUCCESS → tool C 开始/SUCCESS → R SUCCESS。
A/B 是同一 logical_call_id，attempt=1/2，但 observation_id 不同。C 与请求它的 generation
共享调用关联，挂在执行轮次 span 下作为同级操作，不把已结束 generation 作为执行工具的父操作。
例子可省略轮次 span，A/B/C 直接挂根节点；多轮实际埋点应建立轮次 span。
工具重试同理；参数修正后的新 Tool Call 是新的 logical_call_id，不能算原调用的基础设施重试。
拒绝执行的工具请求也记录 ERROR 并标 execution_started=false，不能伪称沙箱已经执行。

## 7. 完整性与成本

Trace 完整性和 Task 成功是两件事。T1 存储/清单扩展需提供 capture_status、缺失原因和导出状态。
丢事件不能默默算完整；记录失败不破坏主任务，但必须有独立告警。
Token/费用只累加各实际 generation 尝试，不能把父级聚合与子级再加一遍。
超时未返回 usage 时费用未知，不记 0；费用是版本化价格表估算，不等于账单。
本地 Trace 先保存，Langfuse 导出可失败重试；稳定 observation_id 用于去重。
用户真实看见的页面需要前端回执；服务端结果只能证明服务端交付内容。

## 8. 如何用来排查

先定位 task_id → 找到 trace_id → 查看当轮 generation 输入 → 查看 tool 的实际 Python 参数
→ 核对工具完整输出与模型可见输出 → 对比最终回复与交付文件。
只能还原模型看到的信息与可见行为，不能证明模型内部隐藏思考，也不保证模型重跑完全一致。

## 9. 验收与下一步

见 docs/verification/10_trace_contract.md。先完成 T1，再埋点；当前没有任何采集效果指标。

## 10. 参考

- https://langfuse.com/docs/observability/data-model
- https://langfuse.com/docs/observability/features/observation-types
- https://langfuse.com/docs/observability/best-practices

本地契约不依赖 Langfuse SDK；远端字段映射在 T4 核对实际 SDK 版本。
