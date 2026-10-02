# T4 Langfuse 导出验收

状态：离线用例与真实实例联调均已执行，结果见文末「执行结果（2026-10-02）」。
真实联调使用本地 `.env` 配置的 Langfuse 项目；仓库内不记录任何密钥。

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

## 执行结果（2026-10-02）

### 代码与环境

- 验收分支：`feat/trace-t4-langfuse`，已先与最新 `main`（`f3d25d9`，含一次性容器硬超时、
  动态迭代预算、P0-5 收口）合并，合并提交 `a41e0ae`；测试对齐提交 `f5f107b`。
- 本机：Windows，Docker Desktop 29.7.2（linux 引擎），固定镜像
  `all-in-one-sandbox:1.11.0@sha256:6328d7fd…fcf906e7`；模型 `deepseek-chat`（DeepSeek API）。
- Langfuse：Cloud JP，`LANGFUSE_ENVIRONMENT=development`；`LANGFUSE_REDACT_KEYS=["email","phone"]`。

### 离线

```bash
python -m pytest tests/test_langfuse_exporter.py tests/test_trace_model.py \
    tests/test_trace_store.py tests/test_trace_generation.py tests/test_trace_operations.py
# 29 passed；全量套件 470 passed, 3 skipped（3 项为本机 Windows 无符号链接权限）
```

合并后修正两处与新 `main` 契约不一致的用例（非产品缺陷）：

1. `test_registry_effective_parameters_and_denial` 原用宿主机 `tmp_path` 作 `PermissionManager`
   工作区；权限契约要求 POSIX 容器路径，改为 `/home/gem/workspace`。
2. `test_stages_object_under_workspace` 需接受 T3 新增的输入 `sha256` 字段
   （设计 17 已声明这是明确的记录扩展）。

### 真实任务

通过 `demo/trace_langfuse_acceptance.py`（走 API lifespan，导出线程按部署方式启动）提交
「解析 sales.csv → 按 region 汇总 → 提交产物」的真实任务：

| 项 | 值 |
|---|---|
| 任务 ID | `764e4332eebd4747a88a682d849e3b82` |
| Trace ID | `8233c472e80b08e48e629f07a36758ee` |
| 业务状态 | `SUCCESS`，42.8s 根操作时延，产物 `reports/<task_id>/region_summary.csv`（49 bytes） |
| 本地 ended 操作 | 123（SPAN 104 / GENERATION 14 / TOOL 5），`capture_status=COMPLETE` |
| 本地回执 | 123 条全部 `accepted`（无 uncertain/rejected/exhausted） |
| 远端 observation | 123，missing=0 extra=0；父子关系无孤儿（parent 缺失 0） |
| Token 交叉核对 | 本地计量 total 54356 = 远端 generation `usageDetails.total` 合计 54356 |

内容核对（Langfuse 只读回读）：

- 根 Span `task`：`isRootObservation=true`、`traceName=document-specialist.task`、
  metadata 含 `task_id` 与 `local_status=SUCCESS`，output 为最终交付答案；根操作在任务结束
  后才结束，POST 提前返回不会提前结束。
- Generation `llm.chat`：input 为完整请求（模型名、system/user 消息、工具 Schema），
  output 为模型响应，`modelParameters` 保留 `tool_choice`，`usageDetails={input:1267,output:388,total:1655}`；
  本地未配置单价，故不发送自定义 cost（`costDetails={}`）。
- Tool `tool.execute`：input 含工具名与生效参数（绝对 sandbox 路径 + `oss_key`），
  output 含执行结果；下载 URL 中的 `Signature` 已被脱敏为 `[REDACTED]`，metadata 含 `bytes`。
- 已核对 `cache_tokens` 只留在 metadata，未重复计入总 Token。

### 遗留问题与注意事项

1. 该组织创建于 2026-09-16 之后，`GET /api/public/traces/{id}` 与 `GET /api/public/observations`
   返回 410。脚本化回读必须使用 `GET /api/public/v2/observations?traceId=…&fields=core,basic,io,metadata,model,usage,trace_context`
   并带 `fromStartTime`/`toStartTime` 时间窗（`io/model/usage` 分组默认不返回）。
2. 导出为「每个 ended 操作一次 POST」，实测吞吐约 1 span/秒：123 个操作约需 2 分钟才全部落地。
   首个短窗口内会看到 pending/sending；如需更快收敛，可考虑把同批 Span 合并到一个 OTLP 请求。
3. 进程在根操作 still-sending 时退出，回执保持 `sending`，下次启动按设计转为 `uncertain`
   并继续发送（本轮已实测：上一轮遗留的 2 条在下一轮启动后转为 `accepted`）。
4. `docs/verification/` 与 `docs/design/` 存在编号重叠：Trace 系列 10–14 与一次性容器系列
   10–15 同名编号并存，建议后续统一编号或加前缀（本次未改，避免破坏既有引用）。
5. 观测到的模型名差异：本地配置 `LLM_MODEL=deepseek-chat`，响应中的 `model=deepseek-flash`，
   Langfuse 上 Generation 的 model 取响应值，请求 input 中保留请求值。属预期差异，非缺陷。
6. 未覆盖项：429 退避、5xx/超时转 uncertain、非法 resolve、跨任务串线、超大 payload 引用等
   仅由离线用例覆盖，本轮未构造真实端点故障。
