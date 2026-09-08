"""Deterministic learning demo. No LLM call, Docker or real S3 is involved.

Uses real Orchestrator/Executor/task database/document validation/Memory/Evaluation.
The fake dispatcher and predefined CSV transform are test fixtures, not a sandbox.
"""
import base64
import csv
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agent.executor import Executor
from agent.orchestrator import AgentOrchestrator
from agent.planner import Plan, PlanStep
from agent.runtime import current_run
from evaluation.metrics import summarize
from memory.notes import NoteMemory
from task.sqlite_store import SQLiteTaskStore
from task.task_manager import TaskManager
from tools.base_tool import BaseTool, ToolResult
from tools.document_tool import DocumentTool
from tools.report_tool import ReportTool


class FixtureFiles:
    def __init__(self, root):
        self.root = Path(root)

    def prepare_task(self, task_id):
        path = self.root / task_id
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _path(self, name):
        root = Path(current_run.get().workspace).resolve()
        target = (root / name).resolve()
        if not target.is_relative_to(root):
            raise PermissionError('outside fixture workspace')
        return target

    def write_bytes_file(self, name, data):
        self._path(name).write_bytes(data)

    def read_bytes_file(self, name):
        return self._path(name).read_bytes()


class FixtureStorage:
    def __init__(self):
        self.objects = {}

    def upload_file_content(self, key, content):
        self.objects[key] = content
        return 'https://example.invalid/fixture/' + key


class FixtureTransform(BaseTool):
    name = 'fixture_summary'
    description = 'Predefined test-only CSV total; never executes generated code.'

    def __init__(self, files, output_format='csv'):
        self.files = files
        self.output_format = output_format

    def parameters_schema(self):
        return {'type': 'object', 'properties': {}, 'additionalProperties': False}

    def execute(self):
        rows = list(csv.DictReader(io.StringIO(self.files.read_bytes_file('input.csv').decode())))
        total = sum(int(row['amount']) for row in rows)
        output = io.BytesIO()
        if self.output_format == 'xlsx':
            from openpyxl import Workbook
            book = Workbook()
            book.active.append(['total'])
            book.active.append([total])
            book.save(output)
            book.close()
            data = output.getvalue()
        else:
            data = f'total\n{total}\n'.encode()
        self.files.write_bytes_file('summary.' + self.output_format, data)
        return ToolResult(True, output=f'total={total}')


class FixtureDispatcher:
    """Fake registry boundary; production Schema/permission dispatch has separate tests."""
    def __init__(self, tools):
        self.tools = {tool.name: tool for tool in tools}

    def to_openai_tools(self):
        return [tool.to_openai_schema() for tool in self.tools.values()]

    def execute(self, name, arguments):
        return self.tools[name].execute(**arguments)

    def retry_safe(self, name):
        return self.tools[name].retry_safe


class FixturePlanner:
    def plan(self, user_input):
        return Plan(user_input, [PlanStep('parse'), PlanStep('summarize'), PlanStep('save')])


class FixtureLLM:
    def __init__(self, output_format='csv'):
        self.index = 0
        self.output_format = output_format

    def chat(self, messages, **kwargs):
        actions = [('parse_document', {'filename': 'input.csv'}), ('fixture_summary', {}),
                   ('save_report', {'sandbox_filename': 'summary.' + self.output_format,
                                    'oss_key': current_run.get().report_prefix + '/summary.' + self.output_format})]
        if self.index < len(actions):
            name, args = actions[self.index]
            call = SimpleNamespace(id=f'call_{self.index}', function=SimpleNamespace(
                name=name, arguments=json.dumps(args)))
            message = SimpleNamespace(content=None, tool_calls=[call])
        else:
            message = SimpleNamespace(content='Report saved; see task artifact metadata.', tool_calls=[])
        self.index += 1
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def run_case(root, values=(10, 20, 30), output_format='csv'):
    root = Path(root)
    manager = TaskManager(SQLiteTaskStore(str(root / 'tasks.db')))
    files, storage = FixtureFiles(root / 'files'), FixtureStorage()
    registry = FixtureDispatcher([DocumentTool(files), FixtureTransform(files, output_format),
                                  ReportTool(files, storage)])
    executor = Executor(FixtureLLM(output_format), registry, manager)
    orchestrator = AgentOrchestrator(manager, FixturePlanner(), executor, sandbox=files,
                                     memory=NoteMemory(str(root / 'tasks.db')))
    source = 'amount\n' + ''.join(f'{value}\n' for value in values)
    task = orchestrator.run('读取 input.csv，生成合计报告', inputs=[{
        'filename': 'input.csv', 'content_base64': base64.b64encode(source.encode()).decode()}],
        artifact_requirements={'required': True, 'format': output_format,
                               'required_columns': ['total'], 'min_rows': 1})
    return task, storage, manager


def main():
    with TemporaryDirectory() as directory:
        task, storage, manager = run_case(directory)
        restored = TaskManager(SQLiteTaskStore(str(Path(directory) / 'tasks.db'))).get_task(task.id)
        assert restored.status.value == 'SUCCESS'
        assert next(iter(storage.objects.values())) == b'total\n60\n'
        print(json.dumps({'mode': 'offline fixtures; no real model/sandbox/S3',
                          'status': restored.status.value,
                          'steps': [step.name for step in restored.steps],
                          'artifact': restored.metrics['artifacts'][0],
                          'evaluation': summarize(manager.list_tasks())}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
