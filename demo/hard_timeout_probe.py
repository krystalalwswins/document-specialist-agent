"""Capability probe: is shell `hard_timeout` a real execution bound?

Diagnostic only. This probe does not touch the Harness execution path; it talks
to the AIO Sandbox shell API directly and measures:

1. whether a command that exceeds `hard_timeout` is really stopped
   (2s, 6s, 12s payloads against 1s, and 12s against 8s),
2. whether an ordinary child process is cleaned up together with its parent,
3. whether a background (`&`) process survives,
4. whether a process started with `start_new_session=True` survives,
5. how long the call actually takes relative to `hard_timeout`,
6. what `success / status / exit_code / output` look like on a hard timeout,
7. whether a hard timeout poisons the next command in the same session,
8. whether marker files or tagged processes remain afterwards.

Exit codes: 0 all required scenarios were contained, 1 a required scenario
leaked, 2 NOT VERIFIED (sandbox unreachable).
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

# The scanner excludes its own cmdline (and the shell that launched it) by
# ignoring any cmdline that contains the marker below.
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

# Used only for cleanup: kill everything still carrying a probe tag so the
# container is left as we found it.
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
    command: str
    hard_timeout: float
    payload_sleep: float
    marker: str
    tag: str
    mode: str  # "required" must be contained, "boundary" is informational
    note: str
    result: dict = field(default_factory=dict)


def payload_code(marker: str, sleep_seconds: float, linger_seconds: float = 0.0) -> str:
    """Python that sleeps, writes the marker, then optionally keeps running."""
    return (
        "import time\n"
        "from pathlib import Path\n"
        f"time.sleep({sleep_seconds!r})\n"
        f"Path({marker!r}).write_text('late side effect')\n"
        "print('payload finished', flush=True)\n"
        + (f"time.sleep({linger_seconds!r})\n" if linger_seconds else "")
    )


def build_scenarios(root: str) -> list[Scenario]:
    def tag_for(name: str) -> str:
        return "htprobe-" + name + "-" + uuid.uuid4().hex[:8]

    def marker_for(tag: str) -> str:
        return root + "/" + tag + ".txt"

    scenarios: list[Scenario] = []

    # 1-4: current process, the four control combinations.
    for index, (payload_sleep, hard_timeout) in enumerate(
        [(2.0, 1.0), (6.0, 1.0), (12.0, 1.0), (12.0, 8.0)], start=1
    ):
        tag = tag_for("current%d" % index)
        marker = marker_for(tag)
        code = payload_code(marker, payload_sleep)
        scenarios.append(
            Scenario(
                name="current-process sleep=%.0fs hard_timeout=%.0fs" % (payload_sleep, hard_timeout),
                command="python3 -c %s %s" % (shlex.quote(code), shlex.quote(tag)),
                hard_timeout=hard_timeout,
                payload_sleep=payload_sleep,
                marker=marker,
                tag=tag,
                mode="required",
                note="the running command must stop before its delayed write",
            )
        )

    # 5: ordinary child process; the parent waits, so both are alive at the
    # hard timeout and the child shares the parent's process group.
    tag = tag_for("child")
    marker = marker_for(tag)
    child_code = payload_code(marker, 3.0)
    parent_code = (
        "import subprocess, sys\n"
        "result = subprocess.run([sys.executable, '-c', %r, %r])\n"
        "result.check_returncode()\n"
        "print('parent finished')\n" % (child_code, tag)
    )
    scenarios.append(
        Scenario(
            name="ordinary-child sleep=3s hard_timeout=1s",
            command="python3 -c %s %s" % (shlex.quote(parent_code), shlex.quote(tag)),
            hard_timeout=1.0,
            payload_sleep=3.0,
            marker=marker,
            tag=tag,
            mode="required",
            note="child shares the process group; it must not outlive the kill",
        )
    )

    # 6: detached child (new session) while the parent stays alive.
    tag = tag_for("detached")
    marker = marker_for(tag)
    child_code = payload_code(marker, 3.0, linger_seconds=30.0)
    parent_code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', %r, %r], start_new_session=True)\n"
        "time.sleep(30)\n" % (child_code, tag)
    )
    scenarios.append(
        Scenario(
            name="detached-child start_new_session=True sleep=3s hard_timeout=1s",
            command="python3 -c %s %s" % (shlex.quote(parent_code), shlex.quote(tag)),
            hard_timeout=1.0,
            payload_sleep=3.0,
            marker=marker,
            tag=tag,
            mode="boundary",
            note="new session; likely escapes a process-group kill",
        )
    )

    # 7: shell background job; the command returns before the payload runs.
    tag = tag_for("background")
    marker = marker_for(tag)
    child_code = payload_code(marker, 3.0, linger_seconds=30.0)
    scenarios.append(
        Scenario(
            name="shell-background sleep=3s hard_timeout=1s",
            command="python3 -c %s %s & echo background-started"
            % (shlex.quote(child_code), shlex.quote(tag)),
            hard_timeout=1.0,
            payload_sleep=3.0,
            marker=marker,
            tag=tag,
            mode="boundary",
            note="command returns immediately, so hard_timeout never fires",
        )
    )

    # 8: current process that keeps running after the write. Checking the
    # process table *immediately* after the hard timeout separates "the command
    # was killed" from "the API call merely stopped waiting".
    tag = tag_for("linger")
    marker = marker_for(tag)
    code = payload_code(marker, 3.0, linger_seconds=30.0)
    scenarios.append(
        Scenario(
            name="current-process-lingering sleep=3s linger=30s hard_timeout=1s",
            command="python3 -c %s %s" % (shlex.quote(code), shlex.quote(tag)),
            hard_timeout=1.0,
            payload_sleep=3.0,
            marker=marker,
            tag=tag,
            mode="required",
            note="if the process is alive right after the timeout, nothing was killed",
        )
    )

    return scenarios


def shell(client: Sandbox, command: str, workspace: str, timeout: float):
    """Helper commands run without a hard timeout; None must not be sent."""
    return client.shell.exec_command(
        command=command,
        exec_dir=workspace,
        timeout=timeout,
        request_options={"timeout_in_seconds": timeout + 10, "max_retries": 0},
    )


def run_scenario(client: Sandbox, scenario: Scenario, workspace: str) -> None:
    response = client.shell.exec_command(
        command=scenario.command,
        exec_dir=workspace,
        timeout=scenario.hard_timeout + 20,
        hard_timeout=scenario.hard_timeout,
        request_options={
            "timeout_in_seconds": scenario.hard_timeout + 30,
            "max_retries": 0,
        },
    )
    elapsed = time.monotonic() - scenario.result.get("started", time.monotonic())
    data = getattr(response, "data", None)
    scenario.result.update(
        {
            "success": getattr(response, "success", None),
            "message": getattr(response, "message", None),
            "status": getattr(data, "status", None),
            "exit_code": getattr(data, "exit_code", None),
            "output": (getattr(data, "output", None) or "").strip(),
            "session_id": getattr(data, "session_id", None),
            "elapsed": elapsed,
            "overhead": elapsed - scenario.hard_timeout,
        }
    )

    remaining = scenario.payload_sleep + PROBE_MARGIN_SECONDS - elapsed

    # Snapshot the process table before waiting: this is what separates "the
    # command was killed" from "the API call merely stopped waiting".
    immediate = shell(
        client,
        "python3 -c %s %s" % (shlex.quote(PROCSCAN_SCRIPT), shlex.quote(scenario.tag)),
        workspace,
        timeout=15,
    )
    immediate_lines = ((getattr(immediate, "data", None) and immediate.data.output) or "").strip().splitlines()
    scenario.result["residual_immediately"] = (
        int(immediate_lines[0]) if immediate_lines and immediate_lines[0].isdigit() else -1
    )

    if remaining > 0:
        time.sleep(remaining)

    marker = shell(
        client,
        "test -f %s && echo present || echo absent" % shlex.quote(scenario.marker),
        workspace,
        timeout=10,
    )
    scenario.result["marker"] = ((getattr(marker, "data", None) and marker.data.output) or "").strip()

    scan = shell(
        client,
        "python3 -c %s %s" % (shlex.quote(PROCSCAN_SCRIPT), shlex.quote(scenario.tag)),
        workspace,
        timeout=15,
    )
    lines = ((getattr(scan, "data", None) and scan.data.output) or "").strip().splitlines()
    scenario.result["residual_processes"] = int(lines[0]) if lines and lines[0].isdigit() else -1
    scenario.result["residual_details"] = lines[1:]

    session_id = scenario.result.get("session_id")
    if session_id:
        isolation = shell(client, "echo isolation-ok", workspace, timeout=10)
        isolation_data = getattr(isolation, "data", None)
        scenario.result["isolation"] = {
            "status": getattr(isolation_data, "status", None),
            "exit_code": getattr(isolation_data, "exit_code", None),
            "output": ((getattr(isolation_data, "output", None) or "").strip()),
        }

    contained = (
        scenario.result.get("status") == "hard_timeout"
        and scenario.result.get("marker") == "absent"
        and scenario.result.get("residual_processes") == 0
        and scenario.result.get("residual_immediately") == 0
    )
    scenario.result["contained"] = contained


def report(scenario: Scenario) -> None:
    result = scenario.result
    print("--- %s [%s] ---" % (scenario.name, scenario.mode))
    print("  expectation : %s" % scenario.note)
    print(
        "  returned    : success=%r status=%r exit_code=%r elapsed=%.2fs overhead=%+.2fs"
        % (
            result.get("success"),
            result.get("status"),
            result.get("exit_code"),
            result.get("elapsed", 0.0),
            result.get("overhead", 0.0),
        )
    )
    print("  message     : %r" % result.get("message"))
    print("  output      : %r" % result.get("output"))
    print(
        "  processes   : immediately_after_timeout=%s after_wait=%s session=%s"
        % (
            result.get("residual_immediately"),
            result.get("residual_processes"),
            result.get("session_id"),
        )
    )
    print("  after wait  : marker=%s" % result.get("marker"))
    for detail in result.get("residual_details") or []:
        print("                 residual: %s" % detail)
    print("  isolation   : %r" % result.get("isolation"))
    print("  verdict     : %s" % ("CONTAINED" if result.get("contained") else "LEAKED"))


def main() -> int:
    settings = get_settings()
    workspace = settings.sandbox_workspace
    headers = {"X-AIO-API-Key": settings.sandbox_api_key} if settings.sandbox_api_key else None
    client = Sandbox(base_url=settings.sandbox_base_url, headers=headers)

    try:
        client.shell.exec_command(command="echo probe-ready", request_options={"timeout_in_seconds": 20, "max_retries": 0})
    except Exception as exc:  # pragma: no cover - environment check
        print("NOT VERIFIED: sandbox shell is unreachable: %s: %s" % (type(exc).__name__, exc))
        return 2

    root = workspace + "/hard-timeout-probe-" + uuid.uuid4().hex[:8]
    shell(client, "mkdir -p %s" % shlex.quote(root), workspace, timeout=10)

    scenarios = build_scenarios(root)
    sessions: list[str] = []
    print("sandbox base_url : %s" % settings.sandbox_base_url)
    print("workspace        : %s" % workspace)
    print("probe root       : %s" % root)
    print("scenarios        : %d" % len(scenarios))
    print()

    try:
        for scenario in scenarios:
            scenario.result["started"] = time.monotonic()
            try:
                run_scenario(client, scenario, workspace)
            except Exception as exc:  # pragma: no cover - diagnostic path
                scenario.result.setdefault("status", "exception")
                scenario.result["exception"] = "%s: %s" % (type(exc).__name__, exc)
                print("--- %s [%s] ---" % (scenario.name, scenario.mode))
                print("  raised      : %s" % scenario.result["exception"])
                print("  verdict     : LEAKED (no clean containment observed)")
                continue
            sessions.append(scenario.result.get("session_id"))
            report(scenario)
            print()

        required = [s for s in scenarios if s.mode == "required"]
        boundary = [s for s in scenarios if s.mode == "boundary"]
        required_leaks = [s.name for s in required if not s.result.get("contained")]
        boundary_leaks = [s.name for s in boundary if not s.result.get("contained")]
        overheads = [s.result["overhead"] for s in scenarios if "overhead" in s.result]

        print("=== summary ===")
        print("required contained : %d/%d" % (len(required) - len(required_leaks), len(required)))
        print("boundary contained : %d/%d" % (len(boundary) - len(boundary_leaks), len(boundary)))
        if overheads:
            print("overhead over hard_timeout (s): min=%+.2f max=%+.2f" % (min(overheads), max(overheads)))
        for name in required_leaks:
            print("REQUIRED LEAK      : %s" % name)
        for name in boundary_leaks:
            print("boundary leak      : %s" % name)
        print()
        print("=== capability boundary ===")
        if required_leaks:
            print("hard_timeout did NOT contain every required scenario.")
            print("Conclusion: do not switch execute_python onto shell+hard_timeout yet.")
        else:
            print("hard_timeout contained the running command and the ordinary child process.")
            if boundary_leaks:
                print("Background and/or detached processes still escaped:")
                for name in boundary_leaks:
                    print("  - %s" % name)
                print("Conclusion: containment is incomplete; a session/container level kill")
                print("is still required before this can back execute_python.")
            else:
                print("Background and detached processes were contained as well.")
                print("Next: audit document_tool / sandbox hooks for Jupyter-only output")
                print("dependencies before designing the migration.")
        return 1 if required_leaks else 0
    finally:
        # Leave the container as we found it: kill every tagged leftover, then
        # remove every marker file.
        for probe_scenario in scenarios:
            try:
                shell(
                    client,
                    "python3 -c %s %s"
                    % (shlex.quote(KILL_SCRIPT), shlex.quote(probe_scenario.tag)),
                    workspace,
                    timeout=15,
                )
            except Exception:
                pass
            try:
                shell(
                    client,
                    "rm -f %s" % shlex.quote(probe_scenario.marker),
                    workspace,
                    timeout=10,
                )
            except Exception:
                pass
        for session_id in sessions:
            if session_id:
                try:
                    client.shell.cleanup_session(session_id, request_options={"timeout_in_seconds": 15, "max_retries": 0})
                except Exception:
                    pass
        try:
            shell(client, "rmdir %s 2>/dev/null || true" % shlex.quote(root), workspace, timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
