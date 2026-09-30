# T4 待执行验收

状态：提供验收资产，未执行测试，未发送真实 Langfuse 请求。真实验证需要项目密钥与可达实例。

## 离线命令（交给 Codex 执行）

```bash
python -m pytest tests/test_langfuse_exporter.py tests/test_trace_model.py tests/test_trace_store.py tests/test_trace_generation.py tests/test_trace_operations.py
```

新增用例验证 HTTP 请求头/部分接收、人工回执处理、映射、根生命周期、重启回执、429 退避、超时停止盲目重发、崩溃发送意图、
永久拒绝、脱敏、大内容引用、关闭开关、历史任务不自动上传及目标绑定。
以下是待执行验收矩阵，包含已有离线用例及需要补充的 API/真实运行检查：

| 场景 | 应检查 |
|---|---|
| API lifespan | 启停导出线程，无密钥可启动业务；关闭开关无网络调用 |
| HTTP mock | Basic Auth、OTLP JSON、v4 header、timeout、禁止跳转 |
| 401/403 | rejected；业务 Task 状态不受影响 |
| 429 | 退避达到上限后 exhausted；主任务持续运行 |
| 5xx/超时/部分接收 | uncertain，不重复发送已可能入库的操作 |
| 进程重启 | sending 转 uncertain，accepted 不再发，待发送操作继续 |
| resolve | 鉴权；跨 task 的 observation 不可改；非法状态 409 |
| 两任务同时运行 | ID、父子关系、参数及结果不串线 |
| 大结果与凭证 | 本地引用可读；输出不含设定敏感键、项目密钥 |
| 本地磁盘失败 | 日志可见，不改变业务结果；修复磁盘后检查回执 |

## 真实实例验收

1. 依学习文档配置测试项目，准备不含真实用户敏感数据的任务。
2. 执行包含 Planner、Executor 和至少一次 run_python 的任务。
3. 本地查 trace/export，保存 Trace ID。Langfuse 按 ID 找到同一任务。
4. 逐项核对模型实际输入、工具 Schema、代码、实际执行参数、返回、根输出。
5. 比较 ended 本地操作 ID 集合和远端 observation ID 集合；不应缺项或重复。
6. 验证模型重试各自独立；计划、上下文压缩、记忆等只在任务触发相应行为时出现。
7. 费用仅配置单价时核对本地估算；缓存 Token 不重复计入总 Token。
8. 暂时制造不可达端点，确认本地任务完成且导出标记 uncertain；恢复后人工核对再 resolve。
9. 在最终结果之后才有结束的根操作，POST 返回时不能提前结束。
10. 超大 payload 在 Langfuse 明确显示引用/省略，去本地认证接口回读完整记录。

真实结果记录模板：代码提交 / 实例版本 / 测试任务 ID / Trace ID / 本地与远端 ID 数量 /
失败样例 / 截图或只读证据 / 未覆盖项。不要把密钥和含敏感内容的 Trace 提交到仓库。
