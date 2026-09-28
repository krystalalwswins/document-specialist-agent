"""Real Docker/SDK checks, without calling an LLM. Exit 2 means NOT VERIFIED.

Start the project's Docker Compose services before running this module. Uses
unique probe files and only removes files created by this run. No stress bomb.

The timeout probe deliberately separates "what the sandbox reported" from "what
the sandbox actually did": it asserts the terminal-uncertain contract on the
reported result, then waits and checks the workspace for the delayed side
effect no matter how that result was mapped. A delayed file is reported as a
failure even when the status mapping itself looks acceptable.
"""

import json
import shutil
import subprocess
import time
import uuid

from core.config import get_settings
from sandbox.client import SandboxClient, SandboxExecutionUncertain
from security.permission_manager import PermissionDenied
from tools.sandbox_tool import execution_to_tool_result

# The requested timeout is deliberately much smaller than the delay before the
# side effect, so any execution that is not stopped at the requested timeout
# produces the marker file. The delay stays well inside the kill window this
# image was observed to apply, so the failure is reproducible instead of a race
# on the boundary.
PROBE_TIMEOUT_SECONDS = 1
PROBE_DELAY_SECONDS = 3
PROBE_MARGIN_SECONDS = 4


def main():
    if shutil.which("docker") is None:
        print("NOT VERIFIED: docker command is unavailable; no real sandbox checks ran.")
        return 2
    # docker inspect belongs to the host, not the code-execution container.
    inspect = subprocess.run(["docker", "inspect", "doc-agent-sandbox"], capture_output=True, text=True, timeout=15)
    if inspect.returncode:
        print("NOT VERIFIED: start the project's Docker Compose sandbox first.")
        return 2
    container = json.loads(inspect.stdout)[0]
    if not container["State"]["Running"]:
        print("NOT VERIFIED: sandbox container is not running.")
        return 2
    limits = container["HostConfig"]
    assert limits["Memory"] > 0 and limits["NanoCpus"] > 0 and (limits["PidsLimit"] or 0) > 0, "container quotas missing"
    assert limits["MemorySwap"] == limits["Memory"], "unexpected swap policy"
    print("PASS running container quotas:", {k: limits[k] for k in ("Memory", "NanoCpus", "PidsLimit", "MemorySwap")})
    client = SandboxClient(get_settings())
    prefix = "security-probe-" + uuid.uuid4().hex
    filename, marker = prefix + ".txt", prefix + "-late.txt"
    failures = []
    try:
        client.write_text_file(filename, "security probe")
        assert client.read_text_file(filename) == "security probe"
        print("PASS real file write/read")
        try:
            client.read_text_file("../outside")
            raise AssertionError("path traversal accepted")
        except PermissionDenied:
            print("PASS traversal denied")
        assert client.execute_python("security_probe_variable = 1").status == "ok"
        response = client.execute_python("print('security_probe_variable' in globals())")
        assert response.status == "ok" and response.stdout.strip() == "False"
        print("PASS independent Jupyter sessions")
        path = client.resolve(marker)
        code = (
            "import time\n"
            "from pathlib import Path\n"
            f"time.sleep({PROBE_DELAY_SECONDS})\n"
            f"Path({path!r}).write_text('late side effect')\n"
        )
        started = time.monotonic()
        try:
            response = client.execute_python(code, timeout=PROBE_TIMEOUT_SECONDS)
        except SandboxExecutionUncertain as exc:
            # A transport failure or unconfirmed session cleanup is also an
            # execution-uncertain outcome, and the Harness must stop there.
            print("PASS timeout outcome surfaced as execution-uncertain:", exc)
        else:
            elapsed = time.monotonic() - started
            raw_success = getattr(response.raw, "success", None)
            tool_result = execution_to_tool_result(response)
            outcome_checks = [
                ("sandbox reported a non-ok status", response.status != "ok", response.status),
                ("sandbox envelope reported success=false", raw_success is False, raw_success),
                (
                    "client marked the result execution_uncertain",
                    response.execution_uncertain is True,
                    response.execution_uncertain,
                ),
                ("Harness maps it to a failed ToolResult", tool_result.success is False, tool_result.success),
                (
                    "Harness marks it terminal, so it is never replayed",
                    tool_result.terminal is True,
                    tool_result.terminal,
                ),
            ]
            for label, passed, observed in outcome_checks:
                print(("PASS " if passed else "FAIL ") + label + f" (observed={observed!r})")
            print(
                "     status=%r error=%r elapsed=%.2fs"
                % (response.status, response.error, elapsed)
            )
            if not all(passed for _, passed, _ in outcome_checks):
                failures.append("timeout result did not satisfy the terminal-uncertain contract")

        # The delayed file is checked no matter how the status was mapped: this
        # probe observes what the sandbox did, not what it reported.
        remaining = PROBE_DELAY_SECONDS + PROBE_MARGIN_SECONDS - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
        check = client.execute_python(f"from pathlib import Path\nprint(Path({path!r}).exists())", timeout=5)
        assert check.status == "ok", check.text
        if check.stdout.strip() == "True":
            print(
                "FAIL delayed side effect exists: the timed-out execution kept running past "
                f"the requested timeout ({PROBE_TIMEOUT_SECONDS}s) and wrote {path}"
            )
            failures.append("delayed side effect after timeout")
        else:
            print(
                "PASS timeout probe stopped before the delayed write; this does not prove "
                "all child processes are killed"
            )
    finally:
        for probe in (filename, marker):
            client.delete_file(probe)
    if failures:
        print("FAIL real smoke checks:", "; ".join(failures))
        return 1
    print("PASS real smoke checks complete. Save this output with image/SDK versions as verification evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
