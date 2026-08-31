"""HermesHookEngine tests with fakes (no sandbox, no OSS)."""

from types import SimpleNamespace

from core.config import Settings
from sandbox.hooks import HermesHookEngine


class FakeSandbox:
    def __init__(self, read_bytes=b""):
        self.written = {}
        self.deleted = []
        self.read_bytes = read_bytes
        self.executed = []

    def write_bytes_file(self, filename, content):
        self.written[filename] = content
        return f"/home/gem/workspace/{filename}"

    def read_bytes_file(self, filename):
        return self.read_bytes

    def delete_file(self, filename):
        self.deleted.append(filename)

    def execute_python(self, code, timeout=None):
        self.executed.append(code)
        return SimpleNamespace(text="ok")


class FakeStorage:
    def __init__(self):
        self.objects = {}

    def download_file_content(self, key):
        return self.objects[key]

    def upload_file_content(self, key, content):
        self.objects[key] = content
        return f"https://presigned.example/{key}"


def _make(read_bytes=b""):
    sandbox = FakeSandbox(read_bytes=read_bytes)
    storage = FakeStorage()
    engine = HermesHookEngine(
        settings=Settings(_env_file=None),
        sandbox=sandbox,
        storage=storage,
    )
    return engine, sandbox, storage


def test_pre_execution_hook_moves_bytes_oss_to_sandbox():
    engine, sandbox, storage = _make()
    storage.objects["raw/input.xlsx"] = b"\x50\x4b\x03\x04"
    path = engine.pre_execution_hook("raw/input.xlsx", "input.xlsx")
    assert path == "/home/gem/workspace/input.xlsx"
    assert sandbox.written["input.xlsx"] == b"\x50\x4b\x03\x04"


def test_post_execution_hook_persists_and_cleans():
    engine, sandbox, storage = _make(read_bytes=b"result-csv")
    url = engine.post_execution_hook("report.csv", "reports/final.csv")
    assert url == "https://presigned.example/reports/final.csv"
    assert storage.objects["reports/final.csv"] == b"result-csv"
    assert "report.csv" in sandbox.deleted


def test_execute_in_sandbox_returns_text():
    engine, sandbox, _ = _make()
    assert engine.execute_in_sandbox("print(1)") == "ok"
    assert sandbox.executed == ["print(1)"]
