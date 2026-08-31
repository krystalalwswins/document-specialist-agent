# Design Note 01：任务生命周期模块（task/）

## 1. 本次修改解决的工程问题

- **任务不可追踪**：原 `agent_core.py` 是单轮脚本，跑完即丢、失败无痕。引入 `Task`（id + 状态 + 步骤 + 结果），任何时刻都能回答"任务到哪一步了"。
- **状态会被并发改坏**：Agent 服务会被多用户并发调用。状态机集中校验，非法流转抛 `TaskStateError`，杜绝"成功后被标记失败"。
- **存储写死内存**：定义 `TaskStore` 协议，Phase 2 换 Redis 时 `TaskManager` 和调用方不动。

## 2. 完整调用链

```text
Orchestrator / API
  -> TaskManager.create_task(user_input)      -> Task(CREATED)
  -> TaskManager.start_task(id)               -> CREATED -> RUNNING
  -> TaskManager.add_step(id, name, tool)     -> 追加 TaskStep(PENDING)
  -> TaskManager.start_step(id, step_id)      -> PENDING -> RUNNING
  -> TaskManager.succeed_step | fail_step     -> RUNNING -> SUCCESS | FAILED
  -> TaskManager.succeed_task | fail_task     -> RUNNING -> SUCCESS | FAILED

所有写操作：TaskManager(_lock) -> TaskStore.create/get/update
内存实现：InMemoryTaskStore(_lock + dict)
```

## 3. 最关键的代码位置及解释

- `task/task_model.py:34` `TaskStatus` —— 任务级状态枚举，严格对应产品定义。
- `task/task_model.py:73` `TaskStep` —— 步骤是任务可观察/可回放的最小单位。
- `task/task_model.py:91` `TaskStep._transition` —— 步骤状态守卫，非法流转抛错。
- `task/task_model.py:115` / `129` `to_dict` / `from_dict` —— 序列化契约，是换存储和跨进程的前提。
- `task/task_model.py:144` `Task` —— 核心模型；`metrics` 字段为 Phase 2 评估预留。
- `task/task_model.py:166` `Task._transition` —— 任务状态机，含 `_ALLOWED_TRANSITIONS`。
- `task/task_model.py:175` / `178` / `182` `start` / `succeed` / `fail` —— 对外生命周期入口。
- `task/task_model.py:186` `add_step` —— 追加步骤并刷新 `updated_time`。
- `task/task_manager.py:16` `TaskStore` —— 存储协议（ABC），可替换 Redis/DB。
- `task/task_manager.py:36` `InMemoryTaskStore` —— 内存实现，`RLock + dict`。
- `task/task_manager.py:71` `TaskManager` —— 门面，持有 `RLock`，写操作原子化。
- `task/task_manager.py:94` `start_task` 等 —— 生命周期 API，`get -> mutate -> update` 模式。

## 4. 必须掌握的知识点

- 有限状态机：状态 + 合法转移表，非法迁移显式失败。
- `dataclass` 与 `field(default_factory=...)` 避免可变默认值共享。
- ABC 抽象基类做"协议式"设计，面向接口而非实现。
- `to_dict/from_dict` 序列化：跨进程 / 跨存储的契约。
- 线程安全：`RLock` 与竞态窗口。
- UTC 时间戳（`datetime.now(timezone.utc)`）避免时区坑。

## 5. 最容易让我误解的地方

- `created_time` 与 `updated_time` 是两个独立 `default_factory`，初始化时可能相差几微秒，**不能断言相等**。
- `CREATED` 不能直接到 `FAILED`，必须先 `start()` 进入 `RUNNING`——这是设计约束，不是 bug。
- `TaskManager.get_task()` 返回的是内存里同一个对象引用；改了它要再 `update` 才落库，不是"返回副本"。
- `metrics` 现在为空是给 Phase 2 评估预留，不是漏实现。

## 6. 我应该主动回答的 3~5 个问题

- 为什么状态要建模成状态机？→ 异步并发下防脏状态，把合法流转集中在一处。
- 为什么 Manager 和 Store 分开？→ 业务规则 vs 持久化，换存储不改逻辑。
- 为什么 TaskStep 单独成类？→ 让任务可观察、可回放，给评估提供数据。
- 为什么用 dataclass 不用 pydantic？→ 领域模型零依赖、序列化自己掌控；pydantic 留给配置/IO 边界。

## 7. 如何测试本模块

```powershell
.venv\Scripts\python.exe -m pytest tests/test_task_model.py tests/test_task_manager.py -q
```

覆盖：默认值、合法/非法流转、步骤生命周期、dict 往返、20 线程并发冒烟。

## 8. 面试官最可能追问的 5 个问题

- 内存版多进程下怎么办？→ 实现 Redis 版 `TaskStore`，接口不变。
- 并发同时 succeed/fail 会怎样？→ 状态机 + `RLock`，非法流转抛 `TaskStateError`。
- 怎么持久化到 Redis？→ `to_dict` 序列化、`from_dict` 反序列化、`store.update` 覆盖。
- 为什么不允许 `CREATED -> FAILED`？→ 强制走 `RUNNING`，保证生命周期完整、观测一致。
- `duration_ms` 怎么算？→ `fromisoformat` 时间差毫秒取整，异常时返回 None。

## 9. 项目核心代码（必须真正理解）

- `Task._transition` + `_ALLOWED_TRANSITIONS` 状态机。
- `TaskStore` ABC + `TaskManager` 生命周期方法。
- `to_dict / from_dict` 序列化。

## 10. 框架/基础设施细节（暂可不深入）

- `InMemoryTaskStore` 的锁实现细节（会用即可）。
- 各异常类的继承层次（知道抛什么、接什么即可）。
