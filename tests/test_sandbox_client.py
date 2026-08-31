"""SandboxClient tests using a fake SDK (no Docker container required)."""

import base64
from types import SimpleNamespace

from core.config import Settings
from sandbox.client import SandboxClient


class FakeFile:
    def __init__(self):
        self.writes = []
        self.reads = {}
        self.downloads = {}

    def write_file(self, file, content, encoding=None, **kwargs):
        self.writes.append({"file": file, "content": content, "encoding": encoding})
        return SimpleNamespace(
            success=True,
            message="ok",
            data=SimpleNamespace(file=file, bytes_written=len(content)),
        )

    def read_file(self, file, **kwargs):
        content = self.reads.get(file)
        if content is None:
            return SimpleNamespace(success=False, message="not found", data=None)
        return SimpleNamespace(
            success=True,
            message="ok",
            data=SimpleNamespace(content=content, file=file),
        )

    def download_file(self, path, **kwargs):
        return iter(self.downloads.get(path, []))


class FakeShell:
    def __init__(self):
        self.commands = []

    def exec_command(self, command, **kwargs):
        self.commands.append(command)
        return SimpleNamespace(data=SimpleNamespace(output="", exit_code=0))


class FakeJupyter:
    def __init__(self):
        self.calls = []
        self.response = None

    def execute_code(self, code, timeout=None, session_id=None, cwd=None, **kwargs):
        self.calls.append({"code": code, "timeout": timeout, "cwd": cwd})
        return self.response


class FakeSDK:
    def __init__(self):
        self.file = FakeFile()
        self.shell = FakeShell()
        self.jupyter = FakeJupyter()


def _out(output_type, **kwargs):
    defaults = dict(
        output_type=output_type,
        name=None,
        text=None,
        data=None,
        ename=None,
        evalue=None,
        traceback=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make():
    sdk = FakeSDK()
    client = SandboxClient(settings=Settings(_env_file=None), client=sdk)
    return client, sdk


def test_resolve_inside_workspace():
    client, _ = _make()
    assert client.resolve("a.txt") == "/home/gem/workspace/a.txt"
    assert client.resolve("/home/gem/workspace/a.txt") == "/home/gem/workspace/a.txt"


def test_write_text_file_default_encoding():
    client, sdk = _make()
    client.write_text_file("a.txt", "hi")
    write = sdk.file.writes[0]
    assert write["file"] == "/home/gem/workspace/a.txt"
    assert write["content"] == "hi"
    assert write["encoding"] is None


def test_write_bytes_file_uses_base64():
    client, sdk = _make()
    client.write_bytes_file("x.bin", b"\x00\x01\xfe")
    write = sdk.file.writes[0]
    assert write["encoding"] == "base64"
    assert base64.b64decode(write["content"]) == b"\x00\x01\xfe"


def test_read_text_file():
    client, sdk = _make()
    sdk.file.reads["/home/gem/workspace/a.txt"] = "hello"
    assert client.read_text_file("a.txt") == "hello"


def test_read_bytes_file_joins_chunks():
    client, sdk = _make()
    sdk.file.downloads["/home/gem/workspace/x.bin"] = [b"\x00", b"\x01", b"\xff"]
    assert client.read_bytes_file("x.bin") == b"\x00\x01\xff"


def test_delete_file_uses_quoted_rm():
    client, sdk = _make()
    client.delete_file("my file.txt")
    command = sdk.shell.commands[0]
    assert command.startswith("rm -f")
    assert "/home/gem/workspace/my file.txt" in command


def test_execute_python_normalizes_streams_and_results():
    client, sdk = _make()
    sdk.jupyter.response = SimpleNamespace(
        data=SimpleNamespace(
            status="ok",
            outputs=[
                _out("stream", name="stdout", text="hello\n"),
                _out("stream", name="stderr", text="warn\n"),
                _out("execute_result", data={"text/plain": "42"}),
                _out("display_data", data={"text/plain": "chart"}),
            ],
        )
    )
    result = client.execute_python("1+1")
    assert result.status == "ok"
    assert result.stdout == "hello\n"
    assert result.stderr == "warn\n"
    assert result.outputs == ["42", "chart"]
    assert "hello" in result.text
    assert "42" in result.text
    assert "warn" in result.text


def test_execute_python_normalizes_error():
    client, sdk = _make()
    sdk.jupyter.response = SimpleNamespace(
        data=SimpleNamespace(
            status="error",
            outputs=[
                _out(
                    "error",
                    ename="ZeroDivisionError",
                    evalue="division by zero",
                    traceback=["line1", "line2"],
                )
            ],
        )
    )
    result = client.execute_python("1/0")
    assert result.status == "error"
    assert result.error == "ZeroDivisionError: division by zero"
    assert result.traceback == "line1\nline2"
    assert "[error]" in result.text


def test_execute_python_passes_timeout():
    client, sdk = _make()
    sdk.jupyter.response = SimpleNamespace(data=SimpleNamespace(status="ok", outputs=[]))
    client.execute_python("sleep(10)", timeout=5)
    assert sdk.jupyter.calls[0]["timeout"] == 5


def test_execute_python_handles_missing_data():
    client, sdk = _make()
    sdk.jupyter.response = SimpleNamespace(success=False, message="kernel died", data=None)
    result = client.execute_python("boom")
    assert result.status == "error"
    assert result.error == "kernel died"


def test_api_key_header(monkeypatch):
    captured = {}

    class FakeSDKClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("sandbox.client.Sandbox", FakeSDKClient)
    SandboxClient(settings=Settings(_env_file=None, sandbox_api_key="secret"))
    assert captured["headers"] == {"X-AIO-API-Key": "secret"}


def test_no_api_key_header(monkeypatch):
    captured = {}

    class FakeSDKClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("sandbox.client.Sandbox", FakeSDKClient)
    SandboxClient(settings=Settings(_env_file=None, sandbox_api_key=""))
    assert captured.get("headers") is None
