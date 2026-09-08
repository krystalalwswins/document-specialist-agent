"""Use real temporary databases, including independent connections."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.orchestrator import AgentOrchestrator
from agent.planner import Plan, PlanStep
from task.sqlite_store import SQLiteTaskStore
from task.task_manager import TaskManager
from task.task_model import TaskStateError, TaskStatus


def test_reopen_preserves_task_step_plan_and_events(tmp_path):
    path = str(tmp_path / 'tasks.db')
    manager = TaskManager(SQLiteTaskStore(path))
    task = manager.create_task('处理文档')
    manager.start_task(task.id)
    manager.save_plan(task.id, {'steps': ['read']})
    step = manager.add_step(task.id, 'read', 'read_file')
    manager.start_step(task.id, step.id)
    manager.add_metric_events(task.id, 'retry_events', [{'attempt': 1}])
    manager.succeed_step(task.id, step.id, '文档内容')
    manager.succeed_task(task.id, {'answer': '完成'})
    restored = TaskManager(SQLiteTaskStore(path)).get_task(task.id)
    assert restored.status == TaskStatus.SUCCESS
    assert restored.steps[0].output == '文档内容'
    assert restored.metrics['plan'] == {'steps': ['read']}
    assert restored.metrics['retry_events'][0]['attempt'] == 1
    assert [e['kind'] for e in restored.metrics['lifecycle_events']] == [
        'task.created', 'task.started', 'plan.saved', 'step.created',
        'step.started', 'step.succeeded', 'task.succeeded']


def test_independent_managers_do_not_lose_updates(tmp_path):
    path = str(tmp_path / 'tasks.db')
    manager = TaskManager(SQLiteTaskStore(path))
    task = manager.create_task('concurrent')
    managers = [TaskManager(SQLiteTaskStore(path)) for _ in range(4)]
    def append(i):
        managers[i % 4].add_metric_events(task.id, 'attempts', [{'i': i}])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(40)))
    assert sorted(e['i'] for e in manager.get_task(task.id).metrics['attempts']) == list(range(40))


def test_transaction_rolls_back(tmp_path):
    store = SQLiteTaskStore(str(tmp_path / 'tasks.db'))
    manager = TaskManager(store)
    task = manager.create_task('rollback')
    with pytest.raises(RuntimeError):
        with store.transaction():
            manager.start_task(task.id)
            raise RuntimeError('interrupt transaction')
    assert manager.get_task(task.id).status == TaskStatus.CREATED


def test_orchestrator_returns_fresh_snapshot_and_rejects_duplicate(tmp_path):
    manager = TaskManager(SQLiteTaskStore(str(tmp_path / 'tasks.db')))
    class Planner:
        def plan(self, user_input):
            return Plan(user_input, [PlanStep('read')])
    class Executor:
        def run(self, *args):
            return 'done'
    orchestrator = AgentOrchestrator(manager, Planner(), Executor())
    task = orchestrator.run('test')
    assert task.status == TaskStatus.SUCCESS
    with pytest.raises(TaskStateError):
        orchestrator.run_task(task.id)
    assert manager.get_task(task.id).status == TaskStatus.SUCCESS
    running = manager.create_task('already owned')
    manager.start_task(running.id)
    with pytest.raises(TaskStateError):
        orchestrator.run_task(running.id)
    assert manager.get_task(running.id).status == TaskStatus.RUNNING
