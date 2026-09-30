# T1：本地 Trace 存储与查询

## 当前状态

新增 TraceStore、TraceRecorder、配置和只读 API。没有接入 Orchestrator、LLMClient 或工具埋点。
普通 Agent 任务尚无新 Trace，查询返回 404 是正常现象。T0/T1 不表示已完成全链路采集。

## 调用链

后续埋点调用 TraceRecorder → 脱敏 → Payload 快照/卸载 → TraceStore 校验事件图 → 原子落盘。
GET /tasks/{task_id}/trace → 确认 Task 存在 → 根据 manifest.task_id 查询 → 返回事件及完整性。
大内容另用 GET /tasks/{task_id}/trace/payloads/{ref}，只读取该任务事件实际引用的 payload。
接口继承现有 X-API-Token，仍不是多租户授权。未配置 API_TOKEN 时沿用现有本地开发行为。

## 文件与一致性

- trace.json：T0 TraceManifest，不改变 schema。
- events.jsonl：逻辑追加事件；物理上每次重写临时文件、flush/fsync、os.replace，避免半行追加。
- payloads/<opaque-ref>.json：脱敏后 JSON 字节，事件存 sha256/size_bytes。
- capture_errors.json：记录失败的类型，不保存可能带秘密的原异常文本。

单进程全局 RLock 覆盖所有 store 实例的读改写，避免线程争抢序号。
此原型使用 O(n) 重写和 task_id 扫描查询；不适合巨大轨迹或多进程 writer。以后可以索引或换存储，当前不伪称高吞吐。
事件成功落盘前先写 payload，崩溃可留下孤立 payload，但不会让已提交事件指向半写文件。
manifest 与首事件不是一个文件事务；中断在两者之间会显示 INCOMPLETE，不被当作完整成功。
文件 fsync 不等于所有系统的断电目录持久性保证；原子替换主要防半文件。
损坏 JSON、序号断裂、非法父子关系会拒绝读取，接口报 503，不静默跳过坏事件。

## 事件规则

父操作必须已开始且未结束；单根；一个 observation 只能开始/结束一次；父操作关闭前子操作必须全部关闭。
同 observation 的 type/name/parent 不变；跨 task/trace、重复序号都拒绝。
每次 store.get 都重校验，重启不依赖内存索引。
COMPLETE 只表示已记录操作都闭合且没有已知捕获错误，不代表用户任务成功，也不代表尚未接入的模块已被采集。
OPEN 表示还有未闭合操作；INCOMPLETE 表示捕获异常、无首事件或被恢复为 INTERRUPTED。

## 失败隔离与恢复

Recorder 公开调用捕获异常，返回 None 并计 failures、发 logger 告警；可以写盘时标 INCOMPLETE。
完全磁盘不可写时无法保证持久化错误标记，此时只有进程计数和独立日志；不能承诺零丢失。
调用者遇到 None 必须继续业务，不重用其他任务 ID。
重启查询可看到 open_observation_ids。只有外部已确认 worker 停止后才能显式调用 interrupt；
倒序结束未闭合子操作与根操作，标 synthetic 和恢复时刻，不伪造实际停止时间。不自动中断活跃任务。

## 脱敏与边界

结构化凭证字段、Bearer、常见 URL 签名/Token 参数及文本 key=value 进行基础脱敏；input/output/metadata/error 都经过策略。
这不是通用秘密/个人信息检测器，任意自由文本、代码内凭证不保证识别。T2/T3 调用方还需限制采集、按业务增加规则。
Payload 哈希针对脱敏后字节。大内容读取会验证大小/哈希。随机引用只在当前 trace 的 input/output 中查找。
路径式 ref 不会读取文件；拒绝 symlink 越界。假设目录不由恶意同机进程并发修改，不构成 OS 沙箱。
Langfuse 没有接入，也不会向外发送数据。

## 使用示例（程序内，非自动埋点）

```python
from observability import TraceStore, TraceRecorder
store = TraceStore('.data/traces')
recorder = TraceRecorder(store)
trace_id = recorder.create(existing_task_id, {'user_input': '分析表格'})
if trace_id is not None:
    root_id = store.get(trace_id)['manifest']['root_id']
    call_id = recorder.start(trace_id, root_id, 'tool', 'read_file', {'path': 'input.csv'})
    recorder.end(trace_id, call_id, output={'text': '...实际返回...'})
    recorder.end(trace_id, root_id, output={'answer': '完成'})
```

示例中 existing_task_id 应来自 TaskManager；否则可用 store.get 查询，但 API 会因任务不存在返回 404。
读取返回值是独立 JSON 对象，修改它不改变落盘事件。

## 验收

见 docs/verification/11_trace_storage.md。沿用约定未运行测试，无真实模型或外部服务调用。
下一步 T2 采集每次模型输入输出；T3 再接实际工具/交付及完整根生命周期。
