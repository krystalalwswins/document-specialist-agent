# T1 验收（待执行）

分支 feat/trace-t1-store，基于 T0 03208c7。
本轮未运行 pytest、真实 LLM、Docker、Langfuse。

```bash
python -m pytest -q tests/test_trace_model.py tests/test_trace_store.py
python -m pytest -q
```

测试资产覆盖：重启读取、大内容脱敏与跨任务隔离、并发序号、结束后禁止新子操作、
显式 INTERRUPTED 恢复、payload 篡改、损坏事件文件、路径拒绝、磁盘写入失败隔离。
待执行者填入实际 commit、环境、输出。以上均不是 PASS 声明。

API 人工验收（先启动现有服务，构造已有 task 对应的 Trace）：
1. 配置 API_TOKEN，缺少/错误 Token 时两个新接口均返回 401。
2. GET /tasks/{id}/trace 返回 manifest/events/capture_status/open_observation_ids。
3. GET /tasks/{id}/trace/payloads/{ref} 返回完整脱敏内容；他人任务 ref 为 404。
4. 未采集 Trace 的现有任务返回 404；损坏本任务轨迹或 payload 返回 503。
5. 查询 API 不创建或修复 Trace，也不改变任务状态。

限制：单进程锁、全量 JSONL 重写及按任务扫描；没有后台根埋点，正常任务目前不会自动产生新 Trace。
