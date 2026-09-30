# T3 验收资产（未执行）

分支 feat/trace-t3-tools，基线 fbdb10a。

```bash
python -m pytest -q tests/test_trace_operations.py tests/test_trace_generation.py tests/test_trace_store.py tests/test_trace_model.py
python -m pytest -q
```

新增资产验证：父子嵌套、实际参数区别于原参数、工具拒绝不生成执行子节点、
捕获异常不改变业务返回、业务异常不被吞掉。

集成验收清单（独立环境执行）：
- 正常任务：核对输入哈希、计划完整快照、模型请求、实际参数、工具结果、产物校验、最终输出。
- 只重试一次：两条 attempt_dispatch，attempt=1/2，共享 tool_call_id，实际执行节点独立。
- 非法计划绑定：有 agent.tool_call 与拒绝事件，无 Registry 实际执行。
- 大输出：工具原始结果与 Hook 预览分别可回读，下一轮模型输入等于实际回注。
- 终止性工具失败：完整错误/terminal 留存，没有新模型调用。
- 局部重规划：旧新计划及原因齐全，失败重规划不出现 applied 快照。
- 压缩/熔断：前后消息与对应 context_events 可核对，Generation 嵌套正确。
- 记忆召回失败：主任务继续，错误事件留存。
- 两个并发 worker：父子关系与任务无串线，所有终态无意外开放子节点。
- 基础脱敏/大内容哈希/损坏数据读取/关闭开关回归。

聚焦：NOT RUN；全量：NOT RUN；真实模型/Docker：NOT RUN；Langfuse 未接入。
请回填实际 commit、环境和原始输出，尤其留意 API/输入装载测试是否需要接受新增 sha256 字段。
