# 任务持久化：先做能读懂的版本

## 目标

服务重启后还能查询任务、步骤、计划、结果和已有调用重试记录。沿用 Task → TaskManager → TaskStore 三层；新增 SQLiteTaskStore，不引入 ORM、Redis 或分布式队列。

SQLite 随 Python 自带，数据库是一个本地文件。默认 `TASK_DB_PATH=data/tasks.sqlite3`，API 和命令行 wiring 自动使用它。测试仍可注入 InMemoryTaskStore。Redis/持久化 worker 保留为后续增强，当前不需要安装。

## 建议阅读顺序

1. `task/task_model.py`：状态有哪些，哪些转换合法。
2. `task/task_manager.py`：每次修改都经过管理器，状态变化同时追加生命周期事件。
3. `task/sqlite_store.py`：将 to_dict 的 JSON 放入 tasks 表，读出时 from_dict 恢复对象。
4. `agent/orchestrator.py`：先占用任务执行权，再规划、保存计划、执行、保存最终状态。
5. `tests/test_sqlite_store.py`：通过真实临时数据库理解重开、并发、回滚。

## 为什么需要事务

只给一个 TaskManager 加锁，挡不住另一个管理器同时读取旧数据再覆盖。SQLite 的 BEGIN IMMEDIATE 将“读取 → 修改 → 保存”包成一个事务；任一环节异常就回滚。嵌套 get/update 使用当前线程的同一连接。参数用 SQL 占位符传入。

为减少概念，本版一行保存整个任务快照，不拆任务表、步骤表和事件表。代价是每次写入整个 JSON，列表读取也没有分页，适合小规模学习原型；高吞吐与大量长文档不是当前性能承诺。

## 事件与快照

metrics.plan 保存结构化计划；metrics.lifecycle_events 保存有序序号、task_id、step_id、时间与状态事件。Executor 已有 retry_events/security_events 随管理器逐次更新一并持久化。步骤 output/error 保留执行结果。这里不是模型隐藏思考过程，也尚未补齐所有 LLM 请求与响应的审计。

SQLite 读取返回新对象。因此 Orchestrator 最后重新读取任务，而不是返回执行前的 CREATED 快照。重复启动在异常处理前被状态机拒绝，避免把另一个调用正在执行的任务错误标为 FAILED。

## 异步执行

MAX_CONCURRENT_TASKS 默认 4。API 先获取信号量名额，再创建任务与线程；满额返回 503，不创建多余任务。后台结束时 finally 释放名额。这是每个 API 实例的并发上限，部署多个进程会各有自己的上限，不是全局限流或持久化队列。

## 明确边界

数据库持久化不等于进程恢复执行。进程被杀时后台线程会丢失，RUNNING 记录可查询但不会自动重放或自动收敛；后续须增加显式恢复策略，避免重复产生文件副作用。任务总时限、工具预算和中断任务终态收敛仍待完成，第二批整体保持 IN_PROGRESS。

沙箱超时缺陷 SEC-001 保持延期。这里的事务与并发控制不修复代码终止问题。

## 验证

2026-09-08：Python 3.12，`python -m pytest -q`，135 passed，1 条第三方依赖弃用警告。新增测试覆盖数据库重新打开、40 次跨管理器并发追加、事务回滚、重复执行拒绝与最新状态返回、API 满额拒绝及名额释放。LLM/Docker/S3 真实集成未在本轮运行。
