"""InputStager: object storage -> sandbox, with prefix and path guards."""

from types import SimpleNamespace

import pytest

from core.config import Settings
from sandbox.inputs import InputStager, InputStagingError
from security.permission_manager import PermissionDenied


class FakeStorage:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.downloads = []

    def download_file_content(self, object_name):
        from storage.storage_manager import StorageError

        self.downloads.append(object_name)
        if object_name not in self.objects:
            raise StorageError(f"download failed '{object_name}': NoSuchKey")
        return self.objects[object_name]


class FakeSandbox:
    def __init__(self):
        self.writes = []

    def write_bytes_file(self, filename, content):
        self.writes.append({"filename": filename, "content": content})
        return f"/home/gem/workspace/{filename}"


def _stager(objects=None, **overrides):
    settings = Settings(_env_file=None, **overrides)
    storage, sandbox = FakeStorage(objects), FakeSandbox()
    return InputStager(storage, sandbox, settings), storage, sandbox


def test_stages_object_under_workspace():
    stager, storage, sandbox = _stager({"raw/sales.xlsx": b"xlsx-bytes"})

    record = stager.stage("raw/sales.xlsx")

    assert record == {
        "oss_key": "raw/sales.xlsx",
        "sandbox_path": "/home/gem/workspace/sales.xlsx",
        "filename": "sales.xlsx",
        "bytes": 10,
    }
    assert storage.downloads == ["raw/sales.xlsx"]
    assert sandbox.writes == [{"filename": "sales.xlsx", "content": b"xlsx-bytes"}]


def test_explicit_filename_wins_and_nested_keys_use_the_basename():
    stager, _, sandbox = _stager({"raw/2026/q3/data.csv": b"a,b\n1,2\n"})
    record = stager.stage("raw/2026/q3/data.csv", "custom.csv")
    assert record["sandbox_path"] == "/home/gem/workspace/custom.csv"
    assert sandbox.writes[0]["filename"] == "custom.csv"


@pytest.mark.parametrize("key", ["reports/x.csv", "../raw/x.csv", "/raw/x.csv", "rawx/y.csv"])
def test_keys_outside_the_input_prefix_are_rejected(key):
    stager, storage, sandbox = _stager({"reports/x.csv": b"x"})
    with pytest.raises(PermissionDenied):
        stager.stage(key)
    assert storage.downloads == [] and sandbox.writes == []


@pytest.mark.parametrize("filename", ["../../etc/passwd", "c:\\windows\\x", "..", "/etc/passwd"])
def test_unsafe_destination_names_are_rejected(filename):
    stager, storage, sandbox = _stager({"raw/x.csv": b"x"})
    with pytest.raises(PermissionDenied):
        stager.stage("raw/x.csv", filename)
    assert storage.downloads == [] and sandbox.writes == []


def test_nested_but_safe_names_stay_inside_the_workspace():
    stager, _, sandbox = _stager({"raw/x.csv": b"x"})
    record = stager.stage("raw/x.csv", "incoming/x.csv")
    assert record["sandbox_path"] == "/home/gem/workspace/incoming/x.csv"
    assert sandbox.writes[0]["filename"] == "incoming/x.csv"


def test_sandbox_write_failure_becomes_a_staging_error():
    class BrokenSandbox(FakeSandbox):
        def write_bytes_file(self, filename, content):
            raise RuntimeError("no such directory")

    settings = Settings(_env_file=None)
    stager = InputStager(FakeStorage({"raw/x.csv": b"x"}), BrokenSandbox(), settings)
    with pytest.raises(InputStagingError, match="cannot stage"):
        stager.stage("raw/x.csv", "incoming/x.csv")


def test_missing_object_fails_with_a_clear_error():
    stager, _, sandbox = _stager({})
    with pytest.raises(InputStagingError, match="cannot load input"):
        stager.stage("raw/missing.csv")
    assert sandbox.writes == []


def test_empty_object_is_rejected():
    stager, _, sandbox = _stager({"raw/empty.csv": b""})
    with pytest.raises(InputStagingError, match="empty"):
        stager.stage("raw/empty.csv")
    assert sandbox.writes == []


def test_stage_all_preserves_declared_fields_and_order():
    stager, _, sandbox = _stager({"raw/a.csv": b"aaa", "raw/b.csv": b"bbbb"})
    staged = stager.stage_all(
        [{"oss_key": "raw/a.csv"}, {"oss_key": "raw/b.csv", "filename": "renamed.csv"}]
    )

    assert [item["sandbox_path"] for item in staged] == [
        "/home/gem/workspace/a.csv",
        "/home/gem/workspace/renamed.csv",
    ]
    assert [item["bytes"] for item in staged] == [3, 4]
    assert len(sandbox.writes) == 2
