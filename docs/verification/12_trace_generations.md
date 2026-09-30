# T2 验收（待独立执行）

分支 feat/trace-t2-generation；基线 T1 41297a9。
按此前约定，本轮未运行测试、真实模型或外部服务。

```bash
python -m pytest -q tests/test_trace_generation.py tests/test_trace_model.py tests/test_trace_store.py tests/test_llm_client.py
python -m pytest -q
```

新增验收资产：失败重试独立 observation、可见返回不含隐藏推理、无上下文时禁用、
真实 LLMClient 的请求边界等于保存输入、phase/iteration/prompt_version。
待人工集成验收：
1. 正常 wiring 执行任务后用 /tasks/{id}/trace 查询根和各阶段 generation。
2. 重规划、压缩、记忆提取实际触发时核对各自 phase，未发生不应伪造记录。
3. 两个 worker 并发任务分别查 trace，generation 不串任务；结束后同线程下一个任务不继承前任务。
4. TRACE_ENABLED=false 无新 Trace；写盘故障仍返回正常模型结果并告警。
5. 模型错误最终失败时根结束 ERROR；记忆提取结束后根才结束。
6. 大请求回读哈希一致；输入和输出含敏感字段时基础脱敏有效。

结果：聚焦 NOT RUN；完整 NOT RUN；真实 LLM NOT RUN；Langfuse 未接入。
执行者填写实际 commit、环境、完整输出，不以实现说明替代测试结果。
