"""Feasibility probe: can a one-shot container provide a real hard boundary?

Diagnostic only; the Harness execution path is untouched. Both prior routes are
excluded (see docs/verification/10_hard_timeout_probe.md and
11_cleanup_session_probe.md): neither `hard_timeout` nor `cleanup_session`
stops detached work in time. This probe measures the upper bound of the
remaining option: start a container per execution, force-remove it on timeout.

Measured:

1. after `docker rm -f`, are main / ordinary child / background / detached
   processes really gone,
2. does the host-mounted directory still receive delayed side effects,
3. cold start (run -> executable code) and force-destroy latency,
4. does destroying one container disturb a concurrent one,
5. what happens to a container when the client "crashes" (orphan handling),
6. are memory / CPU / PID / network / mount limits still in force,
7. is the Docker socket kept away from the container.

Exit codes: 0 the one-shot boundary held, 1 a leak was observed, 2 NOT VERIFIED.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

IMAGE = (
    "enterprise-public-cn-beijing.cr.volces.com/vefaas-public/"
    "all-in-one-sandbox:1.11.0"
)
CONTAINER_WORKSPACE = "/home/gem/workspace"
LABEL_KEY = "doc-agent.probe"
PAYLOAD_DELAY_SECONDS = 10.0
POST_DESTROY_WAIT_SECONDS = 15.0
READY_DEADLINE_SECONDS = 90.0

# Runs inside the container: count processes whose cmdline carries the tag.
SCAN_SCRIPT = (
    "import glob, sys\n"
    "tag = sys.argv[1]\n"
    "hits = []\n"
    "for path in glob.glob('/proc/[0-9]*/cmdline'):\n"
    "    try:\n"
    "        raw = open(path, 'rb').read().decode('utf-8', 'replace')\n"
    "    except OSError:\n"
    "        continue\n"
    "    if tag in raw and 'PROCSCAN_MARKER' not in raw:\n"
    "        hits.append(path.split('/')[2])\n"
    "print(len(hits))\n"
)


def find_docker() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(local) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe",
        Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


DOCKER = find_docker()


def docker(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run([DOCKER, *args], capture_output=True, text=True, timeout=timeout)


def payload_code(marker: str, delay: float, linger: float) -> str:
    return (
        "import time\n"
        "from pathlib import Path\n"
        f"time.sleep({delay!r})\n"
        f"Path({marker!r}).write_text('late side effect')\n"
        "print('payload finished', flush=True)\n"
        f"time.sleep({linger!r})\n"
    )


def start_container(name: str, host_dir: Path, run_tag: str, network_args: list[str]) -> dict:
    started = time.monotonic()
    response = docker(
        [
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "%s=%s" % (LABEL_KEY, run_tag),
            "--cpus",
            "2",
            "--memory",
            "4g",
            "--memory-swap",
            "4g",
            "--pids-limit",
            "512",
            *network_args,
            "-v",
            "%s:%s" % (host_dir, CONTAINER_WORKSPACE),
            IMAGE,
        ],
        timeout=180.0,
    )
    record = {
        "started_seconds": time.monotonic() - started,
        "returncode": response.returncode,
        "stderr": response.stderr.strip(),
    }
    if response.returncode:
        record["ready"] = False
        return record

    ready_at = None
    deadline = time.monotonic() + READY_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        probe = docker(["exec", name, "python3", "-c", "print('ready')"], timeout=30.0)
        if probe.returncode == 0 and "ready" in probe.stdout:
            ready_at = time.monotonic()
            break
        time.sleep(0.5)
    record["cold_start_seconds"] = (ready_at - started) if ready_at else None
    record["ready"] = ready_at is not None
    return record


def exec_detached(name: str, argv: list[str]) -> None:
    docker(["exec", "-d", name, *argv], timeout=60.0)


def scan(name: str, tag: str) -> int:
    probe = docker(["exec", name, "python3", "-c", SCAN_SCRIPT, tag], timeout=60.0)
    if probe.returncode:
        return -1
    first = probe.stdout.strip().splitlines()
    return int(first[0]) if first and first[0].isdigit() else -1


def launch_payloads(name: str, run_tag: str, host_dir: Path) -> dict:
    """Start four process shapes, each writing a distinct marker file."""
    markers = {kind: host_dir / ("%s-%s.txt" % (run_tag, kind)) for kind in
               ("main", "child", "background", "detached")}
    container_marker = {kind: "%s/%s-%s.txt" % (CONTAINER_WORKSPACE, run_tag, kind)
                        for kind in markers}

    main_code = payload_code(container_marker["main"], PAYLOAD_DELAY_SECONDS, 60.0)
    exec_detached(name, ["python3", "-c", main_code, run_tag + "-main"])

    child_code = payload_code(container_marker["child"], PAYLOAD_DELAY_SECONDS, 60.0)
    parent_code = (
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', %r, %r])\n" % (child_code, run_tag + "-child")
    )
    exec_detached(name, ["python3", "-c", parent_code, run_tag + "-childparent"])

    background_code = payload_code(container_marker["background"], PAYLOAD_DELAY_SECONDS, 60.0)
    exec_detached(
        name,
        ["sh", "-c", "python3 -c %s %s & echo background-started"
         % (shlex_quote(background_code), shlex_quote(run_tag + "-background"))],
    )

    detached_code = payload_code(container_marker["detached"], PAYLOAD_DELAY_SECONDS, 60.0)
    detached_parent = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', %r, %r], start_new_session=True)\n"
        "time.sleep(60)\n" % (detached_code, run_tag + "-detached")
    )
    exec_detached(name, ["python3", "-c", detached_parent, run_tag + "-detachedparent"])
    return {"markers": markers, "container_markers": container_marker}


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def main() -> int:
    if DOCKER is None:
        print("NOT VERIFIED: docker command is unavailable.")
        return 2
    version = docker(["version", "--format", "{{.Server.Version}}"], timeout=60.0)
    if version.returncode:
        print("NOT VERIFIED: docker engine is not reachable: %s" % version.stderr.strip())
        return 2
    image = docker(["image", "inspect", IMAGE, "--format", "{{.Id}}"], timeout=60.0)
    if image.returncode:
        print("NOT VERIFIED: sandbox image %s is not present locally." % IMAGE)
        return 2

    run_tag = "osprobe-" + uuid.uuid4().hex[:8]
    host_root = Path(".data") / "one-shot-probe" / run_tag
    host_root.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    leaks: list[str] = []

    print("docker            : %s (server %s)" % (DOCKER, version.stdout.strip()))
    print("image             : %s" % image.stdout.strip())
    print("run tag / host dir: %s / %s" % (run_tag, host_root.resolve()))
    print()

    try:
        # --- Scenario 1: containment of every process shape on force-remove ----
        name = "doc-agent-probe-%s-1" % run_tag
        created.append(name)
        start = start_container(name, host_root.resolve(), run_tag, ["-p", "127.0.0.1::8080"])
        print("--- scenario 1: force-remove containment ---")
        print("  start            : %r" % start)
        if not start.get("ready"):
            print("  verdict          : NOT RUN (container did not become ready)")
            leaks.append("scenario1-not-ready")
        else:
            launch_payloads(name, run_tag, host_root.resolve())
            time.sleep(2.0)
            alive = scan(name, run_tag)
            print("  processes alive  : %s (expected >= 4)" % alive)
            destroyed = time.monotonic()
            removal = docker(["rm", "-f", name], timeout=120.0)
            destroy_seconds = time.monotonic() - destroyed
            print("  force destroy    : returncode=%s in %.2fs" % (removal.returncode, destroy_seconds))
            print("  container gone   : %s" % (docker(["ps", "-a", "-q", "-f", "name=%s" % name]).stdout.strip() == ""))
            print("  waiting %.0fs for delayed side effects..." % POST_DESTROY_WAIT_SECONDS)
            time.sleep(POST_DESTROY_WAIT_SECONDS)
            present = [path.name for path in sorted(host_root.glob("%s-*.txt" % run_tag))]
            print("  delayed markers  : %r" % present)
            if present:
                leaks.append("scenario1-delayed-side-effect")
                print("  verdict          : LEAKED (mounted directory received writes)")
            else:
                print("  verdict          : CONTAINED (no delayed side effect after destroy)")
        print()

        # --- Scenario 2: destroying one container must not disturb another ----
        name_a = "doc-agent-probe-%s-2a" % run_tag
        name_b = "doc-agent-probe-%s-2b" % run_tag
        created.extend([name_a, name_b])
        start_a = start_container(name_a, host_root.resolve(), run_tag, ["-p", "127.0.0.1::8080"])
        start_b = start_container(name_b, host_root.resolve(), run_tag, ["-p", "127.0.0.1::8080"])
        print("--- scenario 2: concurrent container isolation ---")
        print("  start a/b        : %r / %r" % (start_a.get("ready"), start_b.get("ready")))
        if start_a.get("ready") and start_b.get("ready"):
            exec_detached(name_b, ["python3", "-c",
                                   payload_code("%s/%s-bystander.txt" % (CONTAINER_WORKSPACE, run_tag),
                                                PAYLOAD_DELAY_SECONDS, 60.0),
                                   run_tag + "-bystander"])
            time.sleep(1.0)
            before = scan(name_b, run_tag + "-bystander")
            docker(["rm", "-f", name_a], timeout=120.0)
            after = scan(name_b, run_tag + "-bystander")
            probe = docker(["exec", name_b, "python3", "-c", "print('b-still-alive')"], timeout=30.0)
            print("  bystander alive  : before=%s after=%s exec_ok=%s" % (before, after, probe.returncode == 0))
            if after < 1 or probe.returncode != 0:
                leaks.append("scenario2-concurrency")
                print("  verdict          : LEAKED (destroying one container disturbed another)")
            else:
                print("  verdict          : ISOLATED")
        else:
            print("  verdict          : NOT RUN")
        print()

        # --- Scenario 3: client crash leaves an orphan; label sweep removes it --
        orphan = "doc-agent-probe-%s-3" % run_tag
        created.append(orphan)
        start_orphan = start_container(orphan, host_root.resolve(), run_tag, ["-p", "127.0.0.1::8080"])
        print("--- scenario 3: orphan container after client crash ---")
        print("  start            : %r" % start_orphan.get("ready"))
        time.sleep(2.0)
        still_there = docker(["ps", "-q", "-f", "name=%s" % orphan]).stdout.strip()
        print("  orphan present   : %s (no supervisor would leave it running)" % bool(still_there))
        sweep_started = time.monotonic()
        listed = docker(["ps", "-aq", "-f", "label=%s=%s" % (LABEL_KEY, run_tag)], timeout=60.0)
        swept = 0
        for container_id in listed.stdout.split():
            docker(["rm", "-f", container_id], timeout=120.0)
            swept += 1
        sweep_seconds = time.monotonic() - sweep_started
        remaining = docker(["ps", "-aq", "-f", "label=%s=%s" % (LABEL_KEY, run_tag)]).stdout.split()
        print("  label sweep      : %d containers removed in %.2fs, remaining=%d"
              % (swept, sweep_seconds, len(remaining)))
        if remaining:
            leaks.append("scenario3-orphan")
            print("  verdict          : LEAKED (orphan survived the sweep)")
        else:
            print("  verdict          : RECOVERABLE (label-based sweep removes orphans)")
        print()

        # --- Scenario 4: limits and docker socket exposure ---------------------
        check_name = "doc-agent-probe-%s-4" % run_tag
        created.append(check_name)
        start_check = start_container(check_name, host_root.resolve(), run_tag, ["-p", "127.0.0.1::8080"])
        print("--- scenario 4: limits and docker socket exposure ---")
        print("  start            : %r" % start_check.get("ready"))
        if start_check.get("ready"):
            inspect = docker(
                ["inspect", check_name, "--format",
                 "{{json .HostConfig}}|{{json .Mounts}}|{{json .NetworkSettings.Ports}}"],
                timeout=60.0,
            )
            host_config_raw, mounts_raw, ports_raw = inspect.stdout.strip().split("|")
            host_config = json.loads(host_config_raw)
            mounts = json.loads(mounts_raw)
            ports = json.loads(ports_raw)
            print("  limits           : memory=%s memory_swap=%s nano_cpus=%s pids=%s network=%s"
                  % (host_config.get("Memory"), host_config.get("MemorySwap"),
                     host_config.get("NanoCpus"), host_config.get("PidsLimit"),
                     host_config.get("NetworkMode")))
            print("  published ports  : %s" % ports)
            print("  mounts           : %s" % [m.get("Source") for m in mounts])
            socket_mounted = any("docker.sock" in str(m.get("Source", "")) for m in mounts)
            socket_visible = docker(["exec", check_name, "sh", "-c", "test -S /var/run/docker.sock && echo yes || echo no"])
            docker_binary = docker(["exec", check_name, "sh", "-c", "command -v docker || echo none"])
            outbound = docker(
                [
                    "exec",
                    check_name,
                    "python3",
                    "-c",
                    "import socket\n"
                    "try:\n"
                    "    socket.create_connection(('1.1.1.1', 53), timeout=4).close()\n"
                    "    print('outbound-reachable')\n"
                    "except Exception as exc:\n"
                    "    print('outbound-blocked: %s' % type(exc).__name__)\n",
                ],
                timeout=30.0,
            )
            print("  docker.sock      : mounted=%s visible_in_container=%s"
                  % (socket_mounted, socket_visible.stdout.strip()))
            print("  docker binary    : %s" % docker_binary.stdout.strip())
            print("  outbound network : %s" % outbound.stdout.strip())
            print("  note             : PID limit is verified by config only; no stress bomb was run")
            prints_ok = (host_config.get("Memory") == 4294967296
                         and host_config.get("MemorySwap") == 4294967296
                         and host_config.get("NanoCpus") == 2000000000
                         and host_config.get("PidsLimit") == 512)
            if not prints_ok or socket_mounted or socket_visible.stdout.strip() != "no":
                leaks.append("scenario4-limits-or-socket")
                print("  verdict          : LEAKED (limits mismatch or docker socket exposed)")
            else:
                print("  verdict          : OK (limits in force, no docker socket in container)")
        else:
            print("  verdict          : NOT RUN")
        print()
    finally:
        for container_id in docker(["ps", "-aq", "-f", "label=%s=%s" % (LABEL_KEY, run_tag)]).stdout.split():
            docker(["rm", "-f", container_id], timeout=120.0)

    print("=== summary ===")
    remaining = docker(["ps", "-aq", "-f", "label=%s=%s" % (LABEL_KEY, run_tag)]).stdout.split()
    print("probe containers left running : %d" % len(remaining))
    if leaks:
        print("leaks/blockers                : %s" % ", ".join(leaks))
        print("=== decision ===")
        print("One-shot containers did not clear every gate; do not design the")
        print("production execution path on them yet.")
        return 1
    print("leaks/blockers                : none")
    print("=== decision ===")
    print("Force-removing a one-shot container removed every process shape and")
    print("left no delayed side effect in the mounted directory.")
    print("Next: audit document_tool / sandbox hooks / Jupyter rich-output deps,")
    print("then cost the migration. Keep Docker control host-side only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
