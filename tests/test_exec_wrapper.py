"""AST last-expression echo and Python 3.10 compatibility of the call wrapper."""

import ast
import subprocess
import sys

from sandbox.exec_wrapper import build_runner_source, split_last_expression


def _run(source: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", build_runner_source(source)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_split_keeps_head_and_last_expression():
    head, expression = split_last_expression("value = 2 + 2\nvalue\n")
    assert head == "value = 2 + 2\n"
    assert expression == "value"


def test_split_handles_statement_on_the_same_line():
    head, expression = split_last_expression("value = 1; value")
    assert head == "value = 1; "
    assert expression == "value"


def test_split_handles_multiline_expression():
    source = "def add(a, b):\n    return a + b\n\nadd(\n    1,\n    2,\n)\n"
    head, expression = split_last_expression(source)
    assert expression == "add(\n    1,\n    2,\n)"
    assert "def add" in head


def test_split_returns_none_without_trailing_expression():
    assert split_last_expression("value = 1\n")[1] is None
    assert split_last_expression("")[1] is None
    assert split_last_expression("def broken(:\n")[1] is None


def test_runner_echoes_last_expression():
    result = _run("2 + 2")
    assert result.returncode == 0
    assert result.stdout.strip() == "4"


def test_runner_stays_silent_for_none():
    result = _run("None")
    assert result.returncode == 0
    assert result.stdout == ""


def test_runner_keeps_stdout_before_the_echo():
    result = _run("print('hello')\n'world'")
    assert result.stdout.splitlines() == ["hello", "'world'"]


def test_runner_does_not_echo_assignments():
    result = _run("value = 5")
    assert result.returncode == 0
    assert result.stdout == ""


def test_runner_reports_exception_with_exit_code_one():
    result = _run("raise ValueError('boom')")
    assert result.returncode == 1
    assert "ValueError" in result.stderr


def test_runner_propagates_system_exit_code():
    result = _run("import sys\nsys.exit(3)")
    assert result.returncode == 3


def test_generated_source_is_python310_compatible():
    ast.parse(build_runner_source("print('x')\n42"), feature_version=(3, 10))


def test_matplotlib_shim_is_guarded_and_forces_agg():
    source = build_runner_source("1")
    assert "_prepare_matplotlib()" in source
    assert 'matplotlib.use("Agg")' in source
