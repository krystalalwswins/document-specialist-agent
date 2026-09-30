# T0 Trace 契约验收

基线：4899a5c；分支：feat/trace-t0-model。
状态：实现与验收资产已提交，沿用用户此前约定，本轮不运行测试。

## 待执行

```bash
python -m pytest -q tests/test_trace_model.py
python -m pytest -q
```

| 验收 | 证据 |
| --- | --- |
| 模型失败重试后执行工具，各次不覆盖 | test_retry_then_tool_and_round_trip |
| JSON 往返 | 同上 |
| 原消息变化不篡改快照 | test_capture_is_a_snapshot |
| 拒绝非法序号、状态、版本和时间 | test_invalid_contract |
| 引用不能是路径，缺失不等于 null | test_payload_contract |

聚焦测试：NOT RUN。完整回归：NOT RUN。真实 LLM/Docker/Langfuse：NOT RUN。
执行者需回填实际 commit、Python/OS、命令和原始结果，不能根据模型构造推断通过。
T0 无 runtime 埋点；无法通过 API 获取新 Trace。跨事件顺序/父子关系验证属于 T1。
