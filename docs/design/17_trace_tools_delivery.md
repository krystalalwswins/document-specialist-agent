# T3：工具、计划、上下文和交付追踪

## 1. 范围与当前状态

基于 T2 fbdb10a，补齐服务端操作证据。实现和验收资产已提交，未运行测试。
不接 Langfuse、不增加前端回执、不运行真实 Docker/模型；已知沙箱超时边界不因追踪而得到修复。

## 2. 调用树与实际参数

根任务 → agent.tool_call → tool.attempt_dispatch → tool.dispatch → tool.execute。
- agent.tool_call：原始 arguments JSON（包括 plan_step_id）、tool_call_id、当前计划版本。
- tool.attempt_dispatch：调用工具的实际第几次尝试，task_step_id/tool_call_id 串联。
- tool.dispatch：Executor 去掉计划绑定参数后的 Registry 入参；所有工具拒绝也会有记录。
- tool.execute：通过 Schema/权限/路径改写之后，真正传给 BaseTool.execute 的 kwargs（包括 cwd/task_id）。
- 每层 ended 记录完整 ToolResult；多层结果是不同边界的证据，不按层数重复计算工具次数。

只有 tool.execute 代表进入了工具实现；dispatch ERROR 且没有 execute 子操作表示在运行前被拒绝。
执行状态未知/超时仍保留 terminal 标志；不声称代码一定停止。实际执行期间的异常保留并原样向上传播。
重试产生不同 observation，attempt_dispatch 的 attempt/task_step_id/tool_call_id 将其关联，工具修参重新调用是新调用。

## 3. 原始输出与模型可见输出

完整工具返回记录在 tool.execute/dispatch ended；输出过大沿用 T1 Payload 卸载与哈希校验。
tool.output_delivery 保存 model_visible_output、task_preview、result_ref 与 TaskStep ID。
它表示 Hook 处理后准备交付的内容；终止性错误可能在回注之前终止任务，所以不能仅靠该快照断言模型已读取。
agent.tool_call 的 last_message 及下一轮 generation 输入才是实际回注/可见性的证据。
模型可见返回不等于工具完整返回；工具完整返回也不等于沙箱所有文件或无限长进程日志。

## 4. 计划与控制调用

TaskManager.set_plan 保存 plan.created 完整结构；replace_plan 保存 before_replan 和 applied 完整结构及原因。
Executor 的 plan.event 记录绑定、完成、拒绝、重规划预算等事件。
complete_plan_step/request_replan 是运行时控制调用，单独 span，不伪装成 Registry 工具执行。
初始 Planner generation 是模型原始输出，plan.created 是运行时校验后的结构。

## 5. 上下文与记忆

context.prepare 输入保存压缩前完整 messages/tools、ContextSnapshot、force_compaction；输出保存处理后 messages。
内部 compaction generation 自动挂在 context.prepare 下；context_events 快照保存预算、压缩/裁剪/熔断事件。
由前后消息可以比较实际保留/移除的信息，非单纯统计长度。
memory.recall 输出是实际注入内容，memory.records 保存召回记录与来源；memory.capture 包含调用与最终事件。
记忆内部捕获的异常可能以事件表示，因此外层 SUCCESS 只表示该方法返回，不能等同记忆操作成功。

## 6. 输入与交付

input.stage 保存声明输入和装载后的路径、字节数；InputStager 为实际下载字节增加 SHA256。
哈希用于确认输入版本，不保存原始二进制输入文件；要做可重跑 Fixture 仍需另存输入对象版本/文件。
artifact.validate 保存产物列表、require_artifact、各项校验结果；校验 ok=false 的操作标 ERROR。
最终模型原始回复在 generation；根 output 是 Task.result，包括服务端答案与产物信息。
不等于前端已经收到/展示。工具存储与模型裁判均未新增业务语义校验。

## 7. 实现方式

observability.operations.traced 装饰现有方法，不改变签名/返回值。
输入工厂在业务前通过 Recorder._safe 构造并立即存快照；输出工厂在业务返回后隔离执行。
函数内部异常记录后原样抛出，追踪工厂异常不跳过业务；输出捕获失败标 INCOMPLETE 并尝试关闭操作。
ContextVar 在操作中绑定父 ID，finally 恢复；模型 generation 自动跟随当前操作父节点。
未绑定 Trace 时装饰器直接执行原函数。新增输入 sha256 是明确的业务记录扩展。

## 8. 状态解读与限制

COMPLETE 表示被记录的操作闭合，不证明未插桩第三方 SDK 内部调用被记录，也不证明业务正确。
瞬时 snapshot 的时间表示记录时间，不应解释为对应业务操作耗时；真正操作 duration_ms 用单调时钟。
当前 generation 按 phase/iteration 关联；计划版本可从对应计划快照与输入关联，没有强制为每条 generation 增加 plan_version 字段。
原始模型请求记录工具 Schema，根 code_version 未配置仍为 null。基础脱敏不保证自由文本秘密全覆盖。
现有 storage 为单进程 O(n) JSONL 重写；多层大结果会增加磁盘开销，优化留给后续。
装饰器按函数签名规范化位置/关键字参数，捕获工厂仍由故障隔离保护。

## 9. 排查顺序

用户输入 → input.stage 文件哈希 → plan.created → generation 实际上下文 →
agent.tool_call 原始代码 → tool.execute 实际代码/cwd → 完整结果 → model_visible_output →
最终 generation → artifact.validate → 根交付内容。

## 10. 验收

见 docs/verification/13_trace_tools_delivery.md。未执行任何测试；不能用文档推断通过。
下一编号 T4 才是 Langfuse 适配。
