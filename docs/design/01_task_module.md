# Design Note 01：任务生命周期模块（task/）

## 模块作用

为 Agent 平台提供**任务生命周期管理**：每个用户请求对应一个 `Task`，带唯一 id、状态机（CREATED → RUNNING → SUCCESS / FAILED）、可追踪的执行步骤（`TaskStep`）和最终结果。`TaskManager` 对外提供完整生命周期 API，`TaskStore` 抽象负责持久化。

## 设计原因（解决什么 Agent 工程问题）

1. **可观测性缺失**：改造前的 `agent_core.py` 是一个单轮脚本，跑完就结束，失败无记录、无法回答"这个任务现在到哪一步了"。
2. **失败无法恢复与诊断**：没有任务记录就没有重试、没有评估数据来源；`error` 字段让 FAILED 状态可解释。
3. **并发/竞态**：Agent 服务可能被多个用户同时调用，状态流转不做约束会出现"成功后被标记失败"之类的脏状态。
4. **存储可替换**：Phase 1 用内存即可演示，但 Phase 2 要换 Redis/DB；因此先定义 `TaskStore` 接口，而不是把 dict 写死在 Manager 里。

## 核心流程

```text
create_task(user_input)
    -> Task(CREATED)
        -> start_task()               RUNNING
            -> add_step / start_step / succeed_step|fail_step   (重复 N 次)
        -> succeed_task(result)       SUCCESS
        -> fail_task(error)           FAILED
```

状态机约束（非法流转抛 `TaskStateError`）：

| 当前状态 | 允许流转 |
| --- | --- |
| CREATED | RUNNING |
| RUNNING | SUCCESS, FAILED |
| SUCCESS / FAILED | 终态，不可再变 |

## 关键代码

状态机守卫（`task/task_model.py`）：

```python
_ALLOWED_TRANSITIONS = {
    TaskStatus.CREATED: {TaskStatus.RUNNING},
    TaskStatus.RUNNING: {TaskStatus.SUCCESS, TaskStatus.FAILED},
}

def _transition(self, target: TaskStatus) -> None:
    allowed = self._ALLOWED_TRANSITIONS.get(self.status, set())
    if target not in allowed:
        raise TaskStateError(...)
    self.status = target
    self._touch()
```

存储抽象（`task/task_manager.py`）：

```python
class TaskStore(ABC):
    def create(self, task: Task) -> None: ...
    def get(self, task_id: str) -> Task: ...
    def list(self) -> list[Task]: ...
    def update(self, task: Task) -> None: ...
```

## 面试回答

**"为什么任务状态要设计成状态机而不是随便改？"**
Agent 任务是异步、可并发的。状态机把"合法流转"集中在一个地方，非法操作（如对已完成任务标记失败）直接被拒绝，避免脏状态；同时 `to_dict/from_dict` 让 Task 可以跨进程持久化，Phase 2 换 Redis 时调用方代码不变。

**"为什么 Manager 和 Store 分开？"**
Manager 负责业务规则（状态流转、步骤追加），Store 负责持久化。面试官问"高并发怎么办"时，答案是：`TaskStore` 是一个协议，内存版给 demo 和单测，Redis 版实现同接口即可，Manager 不动。

**"TaskStep 有什么意义？"**
步骤让任务可观察、可回放；`duration_ms`、`tool` 字段是 Phase 2 Evaluation（平均耗时、工具调用次数）的数据来源。

## 如何测试

`tests/test_task_model.py`：默认值、合法/非法流转、步骤生命周期、dict 往返。
`tests/test_task_manager.py`：完整生命周期、失败路径、自定义 Store 注入、20 线程并发安全冒烟。

运行：`python -m pytest tests/ -q`
