"""Real Docker/S3 smoke, independent of SEC-001 timeout probe. No LLM needed."""
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

from agent.runtime import RunContext, run_scope
from core.config import get_settings
from documents.files import inspect_document
from sandbox.client import SandboxClient
from storage.storage_manager import StorageManager
from task.task_manager import TaskManager
from tools.report_tool import ReportTool


def assert_denied(url):
    try:
        with urlopen(url, timeout=10) as response:
            raise AssertionError(f'expected access denied, received {response.status}')
    except HTTPError as exc:
        assert exc.code in (401, 403), exc.code


def main():
    settings = get_settings()
    sandbox, storage = SandboxClient(settings), StorageManager(settings)
    task_id = uuid.uuid4().hex
    workspace = sandbox.prepare_task(task_id)
    prefix = settings.report_prefix + '/' + task_id
    context = RunContext(task_id, TaskManager(), time.monotonic() + 120, 10,
                         workspace=workspace, report_prefix=prefix)
    keys = []
    with run_scope(context):
        try:
            result = sandbox.execute_python(
                "import csv\nfrom openpyxl import Workbook\n"
                "with open('summary.csv','w',newline='') as f:\n"
                " w=csv.writer(f); w.writerow(['total']); w.writerow([60])\n"
                "b=Workbook(); b.active.append(['total']); b.active.append([60]); b.save('summary.xlsx'); b.close()")
            assert result.status == 'ok', result.text
            for fmt in ('csv', 'xlsx'):
                filename = 'summary.' + fmt
                data = sandbox.read_bytes_file(filename)
                parsed = inspect_document(filename, data, required_columns=['total'])
                assert float(parsed['preview'][1][0]) == 60
                key = prefix + '/' + filename
                keys.append(key)
                report = ReportTool(sandbox, storage, settings.report_prefix).execute(filename, key)
                with urlopen(report.output, timeout=10) as response:
                    assert response.read() == data
                parts = urlsplit(report.output)
                assert_denied(urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')))
                expiring = storage.generate_presigned_url(key, expires_in=1)
                time.sleep(3)
                assert_denied(expiring)
                print('PASS', fmt, 'content, signed download, anonymous denial, expiry')
        finally:
            for filename in ('summary.csv', 'summary.xlsx'):
                sandbox.delete_file(filename)
            for key in keys:
                storage.delete_file(key)


if __name__ == '__main__':
    main()
