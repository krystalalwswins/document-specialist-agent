"""Artifact commit protocol: scan, validate, then write (design note 14 §3.5)."""

import os

import pytest

import sandbox.call_workspace as call_workspace_module
from sandbox.call_workspace import CallWorkspace, CommitError


def _workspace(tmp_path, **kwargs) -> CallWorkspace:
    return CallWorkspace(tmp_path / "calls", **kwargs)


def _task_dir(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir(exist_ok=True)
    return task_dir


def test_prepare_creates_call_directories(tmp_path):
    layout = _workspace(tmp_path).prepare("call-1")
    assert layout.control.is_dir()
    assert layout.out.is_dir()
    assert layout.logs.is_dir()
    assert layout.runner_source_path.name == "run.py"


def test_prepare_rejects_unsafe_call_id(tmp_path):
    with pytest.raises(ValueError):
        _workspace(tmp_path).prepare("../escape")


def test_commit_copies_artifacts_and_reports_final_paths(tmp_path):
    workspace = _workspace(tmp_path)
    layout = workspace.prepare("call-1")
    (layout.out / "report.txt").write_text("data", encoding="utf-8")
    task_dir = _task_dir(tmp_path)

    report = workspace.commit(layout, task_dir, max_files=10, max_total_bytes=1000)

    assert report.ok
    assert report.committed == ["report.txt"]
    assert (task_dir / "report.txt").read_text(encoding="utf-8") == "data"
    assert report.final_paths["report.txt"] == str(task_dir / "report.txt")


def test_identical_target_is_an_idempotent_noop(tmp_path):
    workspace = _workspace(tmp_path)
    layout = workspace.prepare("call-1")
    (layout.out / "report.txt").write_text("same", encoding="utf-8")
    task_dir = _task_dir(tmp_path)
    (task_dir / "report.txt").write_text("same", encoding="utf-8")

    report = workspace.commit(layout, task_dir, max_files=10, max_total_bytes=1000)

    assert report.ok
    assert report.committed == []
    assert report.skipped_identical == ["report.txt"]


def test_refuses_to_overwrite_different_content(tmp_path):
    workspace = _workspace(tmp_path)
    layout = workspace.prepare("call-1")
    (layout.out / "report.txt").write_text("new", encoding="utf-8")
    task_dir = _task_dir(tmp_path)
    (task_dir / "report.txt").write_text("old", encoding="utf-8")

    with pytest.raises(CommitError, match="refusing to overwrite"):
        workspace.commit(layout, task_dir, max_files=10, max_total_bytes=1000)


def test_rejects_symlinked_artifact(tmp_path):
    workspace = _workspace(tmp_path)
    layout = workspace.prepare("call-1")
    target = layout.out / "link.txt"
    try:
        os.symlink("elsewhere", target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")

    with pytest.raises(CommitError, match="symlink"):
        workspace.commit(layout, _task_dir(tmp_path), max_files=10, max_total_bytes=1000)


def test_rejects_reserved_internal_names(tmp_path):
    workspace = _workspace(tmp_path)
    layout = workspace.prepare("call-1")
    (layout.out / "run.py").write_text("print('x')", encoding="utf-8")

    with pytest.raises(CommitError, match="reserved"):
        workspace.commit(layout, _task_dir(tmp_path), max_files=10, max_total_bytes=1000)


def test_rejects_nested_directories_by_default(tmp_path):
    workspace = _workspace(tmp_path)
    layout = workspace.prepare("call-1")
    nested = layout.out / "nested"
    nested.mkdir()
    (nested / "a.txt").write_text("x", encoding="utf-8")

    with pytest.raises(CommitError, match="nesting"):
        workspace.commit(layout, _task_dir(tmp_path), max_files=10, max_total_bytes=1000)


def test_allows_nested_directories_when_enabled(tmp_path):
    workspace = _workspace(tmp_path, allow_nested=True)
    layout = workspace.prepare("call-1")
    nested = layout.out / "nested"
    nested.mkdir()
    (nested / "a.txt").write_text("x", encoding="utf-8")

    report = workspace.commit(layout, _task_dir(tmp_path), max_files=10, max_total_bytes=1000)

    assert report.ok and report.committed == ["nested/a.txt"]


def test_enforces_file_count_budget(tmp_path):
    workspace = _workspace(tmp_path)
    layout = workspace.prepare("call-1")
    for name in ("a.txt", "b.txt"):
        (layout.out / name).write_text("x", encoding="utf-8")

    with pytest.raises(CommitError, match="file count"):
        workspace.commit(layout, _task_dir(tmp_path), max_files=1, max_total_bytes=1000)


def test_enforces_total_byte_budget(tmp_path):
    workspace = _workspace(tmp_path)
    layout = workspace.prepare("call-1")
    (layout.out / "big.txt").write_text("x" * 100, encoding="utf-8")

    with pytest.raises(CommitError, match="byte budget"):
        workspace.commit(layout, _task_dir(tmp_path), max_files=10, max_total_bytes=10)


def test_partial_commit_is_reported_not_hidden(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    layout = workspace.prepare("call-1")
    for name in ("a.txt", "b.txt"):
        (layout.out / name).write_text(name, encoding="utf-8")
    task_dir = _task_dir(tmp_path)

    real_copy = call_workspace_module.shutil.copyfile
    calls = {"count": 0}

    def flaky_copy(source, destination):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("disk full")
        return real_copy(source, destination)

    monkeypatch.setattr(call_workspace_module.shutil, "copyfile", flaky_copy)

    report = workspace.commit(layout, task_dir, max_files=10, max_total_bytes=1000)

    assert report.ok is False
    assert report.committed == ["a.txt"]
    assert "disk full" in report.reason
