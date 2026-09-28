"""Capability probe: does `cleanup_session(session_id)` really terminate work?

Diagnostic only; the Harness execution path is untouched. Given that both
`jupyter.execute_code(timeout=...)` and `shell.exec_command(hard_timeout=...)`
only bound the API response (see docs/verification/10_hard_timeout_probe.md),
the remaining question is whether tearing the shell session down kills the
processes it started.

Measured per scenario:

1. whether the target process tree reaches zero after cleanup,
2. whether the delayed side-effect file stays absent,
3. the window `timeout return -> cleanup -> processes gone`,
4. whether another concurrent session is left untouched,
5. whether the cleaned session is invalidated and can be rebuilt,
6. what cleanup returns for an unknown session id.

Exit codes: 0 every required scenario contained, 1 a required scenario leaked.
"""

from __future__ import annotations

import shlex
import sys
import time
import uuid
from dataclasses import dataclass, field

from agent_sandbox import Sandbox

from core.config import get_settings

PROBE_MARGIN_SECONDS = 3.0
VANISH_DEADLINE_SECONDS = 20.0
VANISH_POLL_SECONDS = 0.25

PROCSCAN_SCRIPT = (
    "import glob, sys\n"
    "tag = sys.argv[1]\n"
    "hits = []\n"
    "for path in glob.glob('/proc/[0-9]*/cmdline'):\n"
    "    try:\n"
    "        raw = open(path, 'rb').read().decode('utf-8', 'replace')\n"
    "    except OSError:\n"
    "        continue\n"
    "    if tag in raw and 'PROCSCAN_MARKER' not in raw:\n"
    "        hits.append(path.split('/')[2] + ' ' + raw.replace(chr(0), ' ').strip())\n"
    "print(len(hits))\n"
    "for hit in hits:\n"
    "    print(hit)\n"
)

KILL_SCRIPT = (
    "import glob, os, signal, sys\n"
    "tag = sys.argv[1]\n"
    "killed = 0\n"
    "for path in glob.glob('/proc/[0-9]*/cmdline'):\n"
    "    try:\n"
    "        raw = open(path, 'rb').read().decode('utf-8', 'replace')\n"
    "    except OSError:\n"
    "        continue\n"
    "    if tag in raw and 'KILLSCAN_MARKER' not in raw:\n"
    "        pid = int(path.split('/')[2])\n"
    "        try:\n"
    "            os.kill(pid, signal.SIGKILL)\n"
    "            killed += 1\n"
    "        except OSError:\n"
    "            pass\n"
    "print('killed=%d' % killed)\n"
)


@dataclass
class Scenario:
    name: str
    session_id: str
    command: str
    tag: str
    marker: str
    payload_sleep: float
    hard_timeout: float
    expect_api_timeout: bool
    mode: str  # required / boundary
    note: str
    result: dict = field(default_factory=dict)


def payload_code(marker: str, sleep_seconds: float, linger_seconds: float) -> str:
    return (
        "import time\n"
        "from pathlib import Path\n"
        f"time.sleep({sleep_seconds!r})\n"
        f"Path({marker!r}).write_text('late side effect')\n"
        "print('payload finished', flush=True)\n"
        f"time.sleep({linger_seconds!r})\n"
    )


def helper(client: Sandbox, workspace: str, command: str, timeout: float = 15.0):
    return client.shell.exec_command(
        command=command,
        exec_dir=workspace,
        timeout=timeout,
        request_options={"timeout_in_seconds": timeout + 10, "max_retries": 0},
    )


def output_of(response) -> str:
    data = getattr(response, "data", None)
    return ((getattr(data, "output", None) or "") if data is not None else "").strip()


def scan(client: Sandbox, workspace: str, tag: str) -> tuple[int, list[str]]:
    response = helper(
        client,
        workspace,
        "python3 -c %s %s" % (shlex.quote(PROCSCAN_SCRIPT), shlex.quote(tag)),
    )
    lines = output_of(response).splitlines()
    count = int(lines[0]) if lines and lines[0].isdigit() else -1
    return count, lines[1:]


def marker_state(client: Sandbox, workspace: str, marker: str) -> str:
    response = helper(
        client,
        workspace,
        "test -f %s && echo present || echo absent" % shlex.quote(marker),
        timeout=10.0,
    )
    return output_of(response)


def build_scenarios(root: str) -> list[Scenario]:
    def tag_for(name: str) -> str:
        return "htclean-" + name + "-" + uuid.uuid4().hex[:8]

    def session_for(name: str) -> str:
        return "htclean-session-" + name + "-" + uuid.uuid4().hex[:8]

    def marker_for(tag: str) -> str:
        return root + "/" + tag + ".txt"

    scenarios: list[Scenario] = []

    # 1: current process, killed through its own session.
    tag = tag_for("current")
    marker = marker_for(tag)
    code = payload_code(marker, 3.0, 30.0)
    scenarios.append(
        Scenario(
            name="current-process sleep=3s linger=30s hard_timeout=1s",
            session_id=session_for("current"),
            command="python3 -c %s %s" % (shlex.quote(code), shlex.quote(tag)),
            tag=tag,
            marker=marker,
            payload_sleep=3.0,
            hard_timeout=1.0,
            expect_api_timeout=True,
            mode="required",
            note="cleanup must terminate the command that timed out",
        )
    )

    # 2: ordinary child process; the parent waits, so the child shares the group.
    tag = tag_for("child")
    marker = marker_for(tag)
    child_code = payload_code(marker, 3.0, 30.0)
    parent_code = (
        "import subprocess, sys\n"
        "result = subprocess.run([sys.executable, '-c', %r, %r])\n"
        "result.check_returncode()\n" % (child_code, tag)
    )
    scenarios.append(
        Scenario(
            name="ordinary-child sleep=3s hard_timeout=1s",
            session_id=session_for("child"),
            command="python3 -c %s %s" % (shlex.quote(parent_code), shlex.quote(tag)),
            tag=tag,
            marker=marker,
            payload_sleep=3.0,
            hard_timeout=1.0,
            expect_api_timeout=True,
            mode="required",
            note="cleanup must also terminate the child process",
        )
    )

    # 3: detached child in its own session; the parent stays alive until kill.
    tag = tag_for("detached")
    marker = marker_for(tag)
    child_code = payload_code(marker, 3.0, 30.0)
    parent_code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', %r, %r], start_new_session=True)\n"
        "time.sleep(30)\n" % (child_code, tag)
    )
    scenarios.append(
        Scenario(
            name="detached-child start_new_session=True sleep=3s hard_timeout=1s",
            session_id=session_for("detached"),
            command="python3 -c %s %s" % (shlex.quote(parent_code), shlex.quote(tag)),
            tag=tag,
            marker=marker,
            payload_sleep=3.0,
            hard_timeout=1.0,
            expect_api_timeout=True,
            mode="boundary",
            note="detached process group; likely escapes a session-level kill",
        )
    )

    # 4: shell background job; the API never times out, only cleanup can stop it.
    tag = tag_for("background")
    marker = marker_for(tag)
    child_code = payload_code(marker, 3.0, 30.0)
    scenarios.append(
        Scenario(
            name="shell-background sleep=3s linger=30s",
            session_id=session_for("background"),
            command="python3 -c %s %s & echo background-started"
            % (shlex.quote(child_code), shlex.quote(tag)),
            tag=tag,
            marker=marker,
            payload_sleep=3.0,
            hard_timeout=1.0,
            expect_api_timeout=False,
            mode="required",
            note="cleanup must stop a background job started in the session",
        )
    )

    return scenarios


def run_scenario(client: Sandbox, scenario: Scenario, workspace: str) -> None:
    created = client.shell.create_session(
        id=scenario.session_id,
        exec_dir=workspace,
        request_options={"timeout_in_seconds": 20, "max_retries": 0},
    )
    scenario.result["create_session"] = {
        "success": getattr(created, "success", None),
        "message": getattr(created, "message", None),
    }

    started = time.monotonic()
    response = client.shell.exec_command(
        command=scenario.command,
        id=scenario.session_id,
        exec_dir=workspace,
        timeout=scenario.hard_timeout + 20,
        hard_timeout=scenario.hard_timeout,
        request_options={"timeout_in_seconds": scenario.hard_timeout + 30, "max_retries": 0},
    )
    api_returned = time.monotonic()
    data = getattr(response, "data", None)
    scenario.result.update(
        {
            "success": getattr(response, "success", None),
            "status": getattr(data, "status", None),
            "exit_code": getattr(data, "exit_code", None),
            "output": (getattr(data, "output", None) or "").strip(),
            "api_seconds": api_returned - started,
        }
    )
    immediate, _ = scan(client, workspace, scenario.tag)
    scenario.result["alive_before_cleanup"] = immediate

    cleanup_started = time.monotonic()
    try:
        cleanup = client.shell.cleanup_session(
            scenario.session_id, request_options={"timeout_in_seconds": 30, "max_retries": 0}
        )
        scenario.result["cleanup"] = {
            "success": getattr(cleanup, "success", None),
            "message": getattr(cleanup, "message", None),
        }
    except Exception as exc:  # pragma: no cover - diagnostic path
        scenario.result["cleanup"] = {"raised": "%s: %s" % (type(exc).__name__, exc)}
    cleanup_returned = time.monotonic()
    scenario.result["cleanup_seconds"] = cleanup_returned - cleanup_started

    deadline = time.monotonic() + VANISH_DEADLINE_SECONDS
    count = immediate
    details: list[str] = []
    while time.monotonic() < deadline:
        count, details = scan(client, workspace, scenario.tag)
        if count == 0:
            break
        time.sleep(VANISH_POLL_SECONDS)
    vanished = time.monotonic()
    scenario.result["vanish_seconds"] = vanished - api_returned
    scenario.result["containment_seconds"] = vanished - started
    scenario.result["alive_after_cleanup"] = count
    scenario.result["residual_details"] = details

    remaining = scenario.payload_sleep + PROBE_MARGIN_SECONDS - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)
    scenario.result["marker"] = marker_state(client, workspace, scenario.marker)

    reuse = try_exec(client, scenario.session_id, workspace, "echo reuse-after-cleanup")
    scenario.result["reuse_after_cleanup"] = reuse

    contained = count == 0 and scenario.result["marker"] == "absent"
    scenario.result["contained"] = contained


def try_exec(client: Sandbox, session_id: str, workspace: str, command: str) -> dict:
    try:
        response = client.shell.exec_command(
            command=command,
            id=session_id,
            exec_dir=workspace,
            timeout=10,
            request_options={"timeout_in_seconds": 20, "max_retries": 0},
        )
    except Exception as exc:
        return {"raised": "%s: %s" % (type(exc).__name__, exc)}
    data = getattr(response, "data", None)
    return {
        "success": getattr(response, "success", None),
        "status": getattr(data, "status", None),
        "output": (getattr(data, "output", None) or "").strip(),
    }


def recreate_and_exec(client: Sandbox, workspace: str, session_id: str) -> dict:
    """exec_command does not auto-create sessions, so create first, then exec."""
    try:
        created = client.shell.create_session(
            id=session_id,
            exec_dir=workspace,
            request_options={"timeout_in_seconds": 20, "max_retries": 0},
        )
    except Exception as exc:
        return {"create_raised": "%s: %s" % (type(exc).__name__, exc)}
    record = {
        "create_success": getattr(created, "success", None),
        "create_message": getattr(created, "message", None),
    }
    record.update(try_exec(client, session_id, workspace, "echo rebuilt-ok"))
    return record


def report(scenario: Scenario) -> None:
    result = scenario.result
    print("--- %s [%s] ---" % (scenario.name, scenario.mode))
    print("  expectation      : %s" % scenario.note)
    print(
        "  api              : success=%r status=%r exit_code=%r api_seconds=%.2f"
        % (result.get("success"), result.get("status"), result.get("exit_code"), result.get("api_seconds", 0.0))
    )
    print("  cleanup          : %r (%.2fs)" % (result.get("cleanup"), result.get("cleanup_seconds", 0.0)))
    print(
        "  alive            : before_cleanup=%s after_cleanup=%s vanish_after_timeout=%.2fs total_boundary=%.2fs"
        % (
            result.get("alive_before_cleanup"),
            result.get("alive_after_cleanup"),
            result.get("vanish_seconds", 0.0),
            result.get("containment_seconds", 0.0),
        )
    )
    for detail in result.get("residual_details") or []:
        print("                     residual: %s" % detail)
    print("  delayed marker   : %s" % result.get("marker"))
    print("  reuse session    : %r" % result.get("reuse_after_cleanup"))
    print("  verdict          : %s" % ("CONTAINED" if result.get("contained") else "LEAKED"))


def main() -> int:
    settings = get_settings()
    workspace = settings.sandbox_workspace
    headers = {"X-AIO-API-Key": settings.sandbox_api_key} if settings.sandbox_api_key else None
    client = Sandbox(base_url=settings.sandbox_base_url, headers=headers)

    try:
        helper(client, workspace, "echo probe-ready", timeout=20.0)
    except Exception as exc:  # pragma: no cover - environment check
        print("NOT VERIFIED: sandbox shell is unreachable: %s: %s" % (type(exc).__name__, exc))
        return 2

    root = workspace + "/cleanup-session-probe-" + uuid.uuid4().hex[:8]
    helper(client, workspace, "mkdir -p %s" % shlex.quote(root), timeout=10.0)

    scenarios = build_scenarios(root)
    sessions: list[str] = []
    print("sandbox base_url : %s" % settings.sandbox_base_url)
    print("workspace        : %s" % workspace)
    print("probe root       : %s" % root)
    print("scenarios        : %d" % len(scenarios))
    print()

    # Bystander session: a concurrent tagged process that cleanup must NOT kill.
    bystander_session = "htclean-session-bystander-" + uuid.uuid4().hex[:8]
    bystander_tag = "htclean-bystander-" + uuid.uuid4().hex[:8]
    bystander_marker = root + "/" + bystander_tag + ".txt"
    bystander_code = payload_code(bystander_marker, 300.0, 0.0)
    isolation: dict = {}

    try:
        created = client.shell.create_session(
            id=bystander_session,
            exec_dir=workspace,
            request_options={"timeout_in_seconds": 20, "max_retries": 0},
        )
        isolation["create"] = getattr(created, "success", None)
        client.shell.exec_command(
            command="python3 -c %s %s & echo bystander-started"
            % (shlex.quote(bystander_code), shlex.quote(bystander_tag)),
            id=bystander_session,
            exec_dir=workspace,
            timeout=20,
            request_options={"timeout_in_seconds": 30, "max_retries": 0},
        )
        time.sleep(1.0)
        isolation["alive_before"] = scan(client, workspace, bystander_tag)[0]
        sessions.append(bystander_session)

        for index, scenario in enumerate(scenarios):
            try:
                run_scenario(client, scenario, workspace)
            except Exception as exc:  # pragma: no cover - diagnostic path
                scenario.result["exception"] = "%s: %s" % (type(exc).__name__, exc)
                print("--- %s [%s] ---" % (scenario.name, scenario.mode))
                print("  raised           : %s" % scenario.result["exception"])
                print("  verdict          : LEAKED (no clean containment observed)")
                print()
                continue
            sessions.append(scenario.session_id)
            report(scenario)
            print()
            if index == 0:
                # The bystander must survive cleaning up a different session.
                isolation["alive_after_target_cleanup"] = scan(client, workspace, bystander_tag)[0]

        # Unknown session id: record what cleanup returns.
        missing = "htclean-missing-" + uuid.uuid4().hex[:8]
        try:
            response = client.shell.cleanup_session(
                missing, request_options={"timeout_in_seconds": 20, "max_retries": 0}
            )
            unknown = {
                "success": getattr(response, "success", None),
                "message": getattr(response, "message", None),
            }
        except Exception as exc:
            unknown = {"raised": "%s: %s" % (type(exc).__name__, exc)}

        # Rebuild check: a brand new id, and the id of a session that was just
        # cleaned up. exec_command alone returns 404 for unknown sessions, so
        # create_session must be used first.
        rebuilt_session = "htclean-session-rebuilt-" + uuid.uuid4().hex[:8]
        rebuilt = recreate_and_exec(client, workspace, rebuilt_session)
        sessions.append(rebuilt_session)
        reused_id = recreate_and_exec(client, workspace, scenarios[0].session_id)

        required = [s for s in scenarios if s.mode == "required"]
        boundary = [s for s in scenarios if s.mode == "boundary"]
        required_leaks = [s.name for s in required if not s.result.get("contained")]
        boundary_leaks = [s.name for s in boundary if not s.result.get("contained")]

        print("=== isolation ===")
        print("  bystander        : %r" % isolation)
        print("=== session lifecycle ===")
        print("  first scenario reuse after cleanup : %r" % scenarios[0].result.get("reuse_after_cleanup"))
        print("  cleanup unknown session            : %r" % unknown)
        print("  rebuild new session                : %r" % rebuilt)
        print("  recreate cleaned session id        : %r" % reused_id)
        print("=== summary ===")
        print("required contained : %d/%d" % (len(required) - len(required_leaks), len(required)))
        print("boundary contained : %d/%d" % (len(boundary) - len(boundary_leaks), len(boundary)))
        for name in required_leaks:
            print("REQUIRED LEAK      : %s" % name)
        for name in boundary_leaks:
            print("boundary leak      : %s" % name)
        windows = [s.result["containment_seconds"] for s in scenarios if "containment_seconds" in s.result]
        if windows:
            print("total boundary (timeout -> processes gone, s): min=%.2f max=%.2f" % (min(windows), max(windows)))
        print()
        print("=== decision ===")
        if required_leaks or boundary_leaks:
            print("cleanup_session did NOT contain every scenario.")
            print("Stop this route; move to one-shot container evaluation.")
        else:
            print("cleanup_session contained every scenario.")
            print("Hard bound is timeout + API return overhead + cleanup latency, not an exact bound.")
        return 1 if (required_leaks or boundary_leaks) else 0
    finally:
        for scenario in scenarios:
            try:
                helper(
                    client,
                    workspace,
                    "python3 -c %s %s" % (shlex.quote(KILL_SCRIPT), shlex.quote(scenario.tag)),
                )
            except Exception:
                pass
            try:
                helper(client, workspace, "rm -f %s" % shlex.quote(scenario.marker), timeout=10.0)
            except Exception:
                pass
        for tag, marker in ((bystander_tag, bystander_marker),):
            try:
                helper(
                    client,
                    workspace,
                    "python3 -c %s %s" % (shlex.quote(KILL_SCRIPT), shlex.quote(tag)),
                )
                helper(client, workspace, "rm -f %s" % shlex.quote(marker), timeout=10.0)
            except Exception:
                pass
        for session_id in sessions + [bystander_session]:
            try:
                client.shell.cleanup_session(
                    session_id, request_options={"timeout_in_seconds": 15, "max_retries": 0}
                )
            except Exception:
                pass
        try:
            helper(client, workspace, "rmdir %s 2>/dev/null || true" % shlex.quote(root), timeout=10.0)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
