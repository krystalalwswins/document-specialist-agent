"""Run with unittest as well as pytest; real SQLite/CSV/XLSX, fake external services."""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.executor import Executor
from agent.orchestrator import AgentOrchestrator
from agent.planner import Plan, PlanStep
from agent.runtime import RunContext, run_scope, current_run, BudgetExceeded
from demo.learning_demo import FixtureDispatcher, FixturePlanner, run_case
from documents.files import decode_inputs, inspect_document, artifact_metadata
from evaluation.metrics import summarize
from evaluation.run import evaluate
from memory.context import compact_messages, result_reference, ContextBudgetExceeded
from memory.notes import NoteMemory, redact
from task.sqlite_store import SQLiteTaskStore
from task.task_manager import TaskManager
from task.task_model import TaskStatus, StepStatus, TaskError
from task.worker import TaskWorkers
from tools.base_tool import BaseTool, ToolResult
from tools.history_tool import HistoryTool
from tools.report_tool import ReportTool


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'tasks.db')
        self.manager = TaskManager(SQLiteTaskStore(self.path))

    def test_csv_and_xlsx_end_to_end(self):
        for fmt in ['csv', 'xlsx']:
            with self.subTest(fmt=fmt):
                task, storage, manager = run_case(Path(self.tmp.name) / fmt, output_format=fmt)
                self.assertEqual(task.status, TaskStatus.SUCCESS)
                self.assertEqual(task.metrics['artifacts'][0]['row_count'], 1)
                self.assertTrue(task.metrics['llm_request_events'])
                self.assertEqual(len(task.steps), 3)

    def test_persistence_in_new_python_process(self):
        task = self.manager.create_task('persist')
        self.manager.start_task(task.id)
        self.manager.succeed_task(task.id, {'answer': 'done'})
        code = ('import sys; from task.sqlite_store import SQLiteTaskStore; '
                'print(SQLiteTaskStore(sys.argv[1]).get(sys.argv[2]).status.value)')
        output = subprocess.check_output([sys.executable, '-c', code, self.path, task.id], text=True)
        self.assertEqual(output.strip(), 'SUCCESS')

    def test_queue_claim_is_unique(self):
        ids = {self.manager.create_task(str(i)).id for i in range(15)}
        managers = [TaskManager(SQLiteTaskStore(self.path)) for _ in range(4)]
        def claim(index):
            task = managers[index % 4].claim_next()
            return task.id if task else None
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed = list(pool.map(claim, range(25)))
        nonempty = [item for item in claimed if item]
        self.assertEqual(set(nonempty), ids)
        self.assertEqual(len(nonempty), 15)

    def test_pending_queue_survives_reopen(self):
        task = self.manager.create_task('queued')
        reopened = TaskManager(SQLiteTaskStore(self.path))
        self.assertEqual(reopened.claim_next().id, task.id)

    def test_queue_capacity_is_atomic(self):
        self.manager.create_task('first', capacity=1)
        with self.assertRaises(TaskError):
            self.manager.create_task('second', capacity=1)
        self.assertEqual(len(self.manager.list_tasks()), 1)

    def test_recovery_fails_active_steps_without_replay(self):
        task = self.manager.create_task('interrupted')
        self.manager.start_task(task.id)
        step = self.manager.add_step(task.id, 'code')
        self.manager.start_step(task.id, step.id)
        pending = self.manager.add_step(task.id, 'later')
        self.assertEqual(self.manager.recover_interrupted(), 1)
        restored = self.manager.get_task(task.id)
        self.assertEqual(restored.status, TaskStatus.FAILED)
        self.assertTrue(all(s.status == StepStatus.FAILED for s in restored.steps))
        self.assertIsNone(self.manager.claim_next())

    def test_worker_handles_failed_task_then_next(self):
        class BrokenExecutor:
            def run(self, *args):
                raise ValueError('test failure')
        orchestrator = AgentOrchestrator(self.manager, FixturePlanner(), BrokenExecutor())
        first = self.manager.create_task('first')
        second = self.manager.create_task('second')
        worker = TaskWorkers(orchestrator, 1)
        with self.assertLogs(level='ERROR'):
            self.assertTrue(worker.run_once())
            self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        self.assertEqual(self.manager.get_task(first.id).status, TaskStatus.FAILED)
        self.assertEqual(self.manager.get_task(second.id).status, TaskStatus.FAILED)

    def test_deadline_stops_before_executor(self):
        class SlowPlanner:
            def plan(self, text):
                current_run.get().deadline = time.monotonic() - 1
                return Plan(text, [PlanStep('step')])
        executor = SimpleNamespace(run=lambda *args: self.fail('executor must not be called'))
        orchestrator = AgentOrchestrator(self.manager, SlowPlanner(), executor)
        with self.assertLogs(level='ERROR'), self.assertRaises(BudgetExceeded):
            orchestrator.run('deadline')
        self.assertEqual(self.manager.list_tasks()[0].status, TaskStatus.FAILED)

    def test_context_is_thread_local(self):
        def run(index):
            context = RunContext(str(index), self.manager, time.monotonic() + 10, 2)
            with run_scope(context):
                return current_run.get().task_id
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(run, range(10))), [str(i) for i in range(10)])
        self.assertIsNone(current_run.get())

    def test_tool_budget_counts_retries(self):
        class Flaky(BaseTool):
            name, description, retry_safe = 'flaky', 'test', True
            def parameters_schema(self): return {'type': 'object'}
            def execute(self): raise ConnectionError('retry')
        task = self.manager.create_task('budget')
        executor = Executor(None, FixtureDispatcher([Flaky()]), self.manager)
        with run_scope(RunContext(task.id, self.manager, time.monotonic() + 10, 1)):
            with patch('agent.executor.time.sleep'), self.assertRaises(BudgetExceeded):
                executor._invoke_tool(task.id, 'flaky', {})
        self.assertEqual(len(self.manager.get_task(task.id).metrics['retry_events']), 1)

    def test_missing_artifact_has_bounded_correction(self):
        class LLM:
            calls = 0
            def chat(self, *args, **kwargs):
                self.calls += 1
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='done', tool_calls=[]))])
        llm = LLM()
        executor = Executor(llm, FixtureDispatcher([]), self.manager)
        orchestrator = AgentOrchestrator(self.manager, FixturePlanner(), executor)
        with self.assertLogs(level='ERROR'), self.assertRaisesRegex(ValueError, 'required_artifact_missing'):
            orchestrator.run('report', artifact_requirements={'required': True})
        self.assertEqual(llm.calls, 3)

    def test_bad_inputs_and_duplicates_rejected(self):
        encoded = base64.b64encode(b'a\n1\n').decode()
        for inputs in [[{'filename': '../x.csv', 'content_base64': encoded}],
                       [{'filename': 'x.csv', 'content_base64': 'invalid!'}],
                       [{'filename': 'x.csv', 'content_base64': encoded}] * 2]:
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                decode_inputs(inputs)

    def test_artifact_requirements_reject_wrong_shape(self):
        for data in [b'', b'a,a\n1,2\n', b'a,b\n1\n', b'a\n']:
            with self.subTest(data=data), self.assertRaises(ValueError):
                inspect_document('out.csv', data)
        with self.assertRaises(ValueError):
            artifact_metadata('out.csv', 'reports/out.csv', b'a\n1\n', {'required_columns': ['missing']})

    def test_invalid_report_is_not_uploaded(self):
        files = SimpleNamespace(read_bytes_file=lambda name: b'')
        storage = SimpleNamespace(upload_file_content=lambda *args: self.fail('must not upload invalid file'))
        with self.assertRaises(ValueError):
            ReportTool(files, storage).execute('out.csv', 'reports/out.csv')

    def test_task_prefix_rejects_other_task(self):
        files = SimpleNamespace(read_bytes_file=lambda name: b'a\n1\n')
        with run_scope(RunContext('one', self.manager, time.monotonic() + 10, 2, report_prefix='reports/one')):
            with self.assertRaises(PermissionError):
                ReportTool(files, None).execute('out.csv', 'reports/two/out.csv')

    def test_context_compaction_preserves_pairs_and_original(self):
        base = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'task'}]
        pair = [{'role': 'assistant', 'tool_calls': [{'id': 'a'}]},
                {'role': 'tool', 'tool_call_id': 'a', 'content': 'x' * 1000}]
        messages = base + pair + [{'role': 'assistant', 'content': 'new result'}]
        compact = compact_messages(messages, 400)
        self.assertEqual(compact, base + [messages[-1]])
        self.assertEqual(len(messages), 5)

    def test_context_overflow_is_explicit(self):
        with self.assertRaises(ContextBudgetExceeded):
            compact_messages([{'role': 'system', 'content': 'x' * 1000}], 100)

    def test_history_reads_only_current_task(self):
        one, two = self.manager.create_task('one'), self.manager.create_task('two')
        step = self.manager.add_step(one.id, 'output')
        self.manager.start_step(one.id, step.id)
        self.manager.succeed_step(one.id, step.id, 'abcdef')
        with run_scope(RunContext(one.id, self.manager, time.monotonic() + 10, 2)):
            self.assertEqual(HistoryTool().execute(step.id, offset=2, limit=2).output, 'cd')
        with run_scope(RunContext(two.id, self.manager, time.monotonic() + 10, 2)):
            with self.assertRaises(TaskError):
                HistoryTool().execute(step.id)
        self.assertIn('read_step_output', result_reference('x' * 3000, one.id, step.id))

    def test_note_redaction_search_and_delete(self):
        memory = NoteMemory(self.path)
        memory.remember('one', '工资报表 salary token=abc user@example.com https://example.com/a')
        matches = NoteMemory(self.path).search('工资报表')
        self.assertEqual(matches[0]['task_id'], 'one')
        self.assertNotIn('abc', matches[0]['note'])
        self.assertNotIn('user@example.com', matches[0]['note'])
        memory.forget('one')
        self.assertEqual(memory.search('工资报表'), [])

    def test_memory_is_opt_in(self):
        memory = NoteMemory(self.path)
        executor = SimpleNamespace(run=lambda *args: 'done')
        orchestrator = AgentOrchestrator(self.manager, FixturePlanner(), executor, memory=memory)
        orchestrator.run('工资 salary')
        self.assertEqual(memory.search('salary'), [])
        task = self.manager.create_task('工资', memory_note='工资 salary 用 CSV')
        orchestrator.run_task(task.id)
        self.assertEqual(memory.search('salary')[0]['task_id'], task.id)

    def test_metrics_empty_and_missing_usage(self):
        self.assertIsNone(summarize([])['success_rate'])
        task = self.manager.create_task('usage')
        self.manager.add_metric_events(task.id, 'llm_events', [{'total_tokens': None}, {'total_tokens': 10}])
        report = summarize(self.manager.list_tasks())
        self.assertEqual(report['usage_missing_calls'], 1)
        self.assertEqual(report['known_total_tokens'], 10)

    def test_fixed_evaluation_cases(self):
        self.assertEqual(evaluate()['pass_rate'], 1.0)


if __name__ == '__main__':
    unittest.main()
