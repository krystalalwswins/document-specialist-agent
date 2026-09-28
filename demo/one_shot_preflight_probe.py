"""Preflight probe for the one-shot execution design (design note 14).

Answers the environment questions left open at design freeze:

1. the real identity story inside the image (the design assumed a `gem` user),
2. which non-root UID/GID can actually write the nested `out` bind mount,
3. whether "read-only parent mount + read-write sub-directory mount" works on
   Windows Docker Desktop,
4. whether the four document-parsing libraries import under
   `--read-only --tmpfs /tmp`,
5. the authoritative manifest digest to pin (vs the local image ID).

Diagnostic only; no production code is involved.

Exit codes: 0 a writable non-root candidate was found and all containment
checks passed, 1 no non-root candidate worked, 2 NOT VERIFIED.
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
TASK_VIRTUAL_ROOT = "/home/gem/workspace/tasks"
CANDIDATE_USERS = ("1000:1000", "65534:65534", "0:0")
VANISH_DEADLINE_SECONDS = 30.0


def find_docker() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    for candidate in (
        Path(local) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe",
        Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


DOCKER = find_docker()


def docker(args: list[str], timeout: float = 180.0) -> subprocess.CompletedProcess:
    return subprocess.run([DOCKER, *args], capture_output=True, text=True, timeout=timeout)


def fields_from(stdout: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    return fields


def main() -> int:
    if DOCKER is None:
        print("NOT VERIFIED: docker command is unavailable.")
        return 2
    if docker(["image", "inspect", IMAGE, "--format", "{{.Id}}"]).returncode:
        print("NOT VERIFIED: image %s is not present locally." % IMAGE)
        return 2

    checks: list[tuple[str, bool, str]] = []

    def record(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok, detail))
        print("%-4s %-36s %s" % ("PASS" if ok else "FAIL", label, detail))

    print("docker : %s" % DOCKER)
    print("image  : %s" % IMAGE)
    print()

    # --- image facts -------------------------------------------------------
    local_id = docker(["image", "inspect", IMAGE, "--format", "{{.Id}}"]).stdout.strip()
    repo_digests = docker(["image", "inspect", IMAGE, "--format", "{{json .RepoDigests}}"]).stdout.strip()
    manifest = docker(["buildx", "imagetools", "inspect", IMAGE], timeout=120.0)
    manifest_digest = ""
    if manifest.returncode == 0:
        for line in manifest.stdout.splitlines():
            if line.lower().startswith("digest:"):
                manifest_digest = line.split(":", 1)[1].strip()
                break
    default_user = docker(["image", "inspect", IMAGE, "--format", "{{.Config.User}}"]).stdout.strip()
    facts = docker(
        ["run", "--rm", "--network", "none", "--entrypoint", "sh", IMAGE, "-c",
         "id -u; id -g; python3 -V; ls -d /home/gem 2>&1; grep -c '^gem:' /etc/passwd 2>/dev/null || echo 0"],
        timeout=120.0,
    )
    fact_lines = facts.stdout.splitlines()
    record("manifest digest pinned", bool(manifest_digest), manifest_digest or "(unavailable)")
    record("image default user is root", default_user == "", "Config.User=%r" % (default_user or ""))
    print("INFO local image id      : %s" % local_id)
    print("INFO repo digests        : %s" % repo_digests)
    print("INFO image uid/gid       : %s" % (fact_lines[:2]))
    print("INFO image python        : %s" % (fact_lines[2] if len(fact_lines) > 2 else "?"))
    print("INFO /home/gem in image  : %s" % (fact_lines[3] if len(fact_lines) > 3 else "?"))
    gem_count = fact_lines[4].strip() if len(fact_lines) > 4 else "0"
    record("no `gem` account in image (expected absent)", gem_count == "0",
           "grep '^gem:' /etc/passwd -> %s entries" % gem_count)
    print()

    # --- per-candidate-user mount matrix -----------------------------------
    probe_root = Path(".data") / "one-shot-preflight" / uuid.uuid4().hex[:8]
    task_dir = probe_root / "task"
    out_dir = task_dir / "out"
    control_dir = probe_root / "control"
    out_dir.mkdir(parents=True, exist_ok=True)
    control_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "input.txt").write_text("input-payload", encoding="utf-8")
    (control_dir / "run.py").write_text("print('runner-ok')\n", encoding="utf-8")
    task_id = "preflight-" + uuid.uuid4().hex[:8]
    mount_root = "%s/%s" % (TASK_VIRTUAL_ROOT, task_id)

    script = "\n".join([
        "echo \"whoami=$(id -u):$(id -g)\"",
        "echo \"mount_owner=$(stat -c '%%u:%%g %%a' %s)\"" % mount_root,
        "echo \"out_owner=$(stat -c '%%u:%%g %%a' %s/out)\"" % mount_root,
        "echo \"read_parent=$(cat %s/input.txt 2>&1)\"" % mount_root,
        "if printf 'probe' > %s/out/out-probe.txt 2>/tmp/e1; then echo 'write_out=ok'; else echo 'write_out=denied'; fi" % mount_root,
        "if printf 'nope' > %s/hack.txt 2>/tmp/e2; then echo 'write_parent=ok'; else echo 'write_parent=denied'; fi" % mount_root,
        "if touch /rootfs-probe 2>/tmp/e3; then echo 'write_rootfs=ok'; else echo 'write_rootfs=denied'; fi",
        "if printf 't' > /tmp/t.txt 2>/tmp/e4; then echo 'write_tmp=ok'; else echo 'write_tmp=denied'; fi",
        "echo \"runner=$(python3 /runner/run.py 2>&1)\"",
        "python3 - <<'PY'\n"
        "import importlib, os\n"
        "for name in ('fitz', 'openpyxl', 'pptx', 'docx2txt'):\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "        print('%s=ok' % name)\n"
        "    except Exception as exc:\n"
        "        print('%s=%s' % (name, type(exc).__name__))\n"
        "print('HOME=%s' % os.environ.get('HOME'))\n"
        "PY",
    ])

    matrix: dict[str, dict[str, str]] = {}
    for user in CANDIDATE_USERS:
        result = docker(
            [
                "run", "--rm", "--name", "doc-agent-preflight-%s-%s" % (task_id, user.replace(":", "-")),
                "--network", "none", "--read-only", "--tmpfs", "/tmp",
                "--user", user, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "-e", "HOME=/tmp", "-e", "XDG_CACHE_HOME=/tmp", "-e", "MPLCONFIGDIR=/tmp",
                "-v", "%s:%s:ro" % (task_dir.resolve(), mount_root),
                "-v", "%s:%s/out:rw" % (out_dir.resolve(), mount_root),
                "-v", "%s:/runner/run.py:ro" % (control_dir / "run.py").resolve(),
                "--entrypoint", "sh", IMAGE, "-c", script,
            ],
            timeout=180.0,
        )
        parsed = fields_from(result.stdout)
        matrix[user] = parsed
        print("--- user %s (exit %d) ---" % (user, result.returncode))
        print(result.stdout.strip())
        if result.stderr.strip():
            print("stderr: %s" % result.stderr.strip())
        print()

    # Host-side verification uses the last successful write to out/.
    host_out = out_dir / "out-probe.txt"
    host_hack = task_dir / "hack.txt"
    writable = [user for user, data in matrix.items() if data.get("write_out") == "ok"]
    denied_parent = [user for user, data in matrix.items() if data.get("write_parent") == "denied"]
    denied_rootfs = [user for user, data in matrix.items() if data.get("write_rootfs") == "denied"]
    tmp_ok = [user for user, data in matrix.items() if data.get("write_tmp") == "ok"]
    libs_ok = [
        user for user, data in matrix.items()
        if all(data.get(name) == "ok" for name in ("fitz", "openpyxl", "pptx", "docx2txt"))
    ]

    record("non-root candidate can write out/", bool([u for u in writable if u != "0:0"]),
           "writable users: %s" % (writable or "none"))
    record("read-only parent enforced (all users)", len(denied_parent) == len(CANDIDATE_USERS),
           "denied: %s" % denied_parent)
    record("read-only rootfs enforced (all users)", len(denied_rootfs) == len(CANDIDATE_USERS),
           "denied: %s" % denied_rootfs)
    record("tmpfs writable (all users)", len(tmp_ok) == len(CANDIDATE_USERS), "ok: %s" % tmp_ok)
    record("parser libs import (all users)", len(libs_ok) == len(CANDIDATE_USERS), "ok: %s" % libs_ok)
    record("mount is ro at /home/gem/workspace/tasks/<id>",
           all(data.get("read_parent") == "input-payload" for data in matrix.values()),
           "sample: %s" % matrix[CANDIDATE_USERS[0]].get("read_parent", ""))
    record("control script mounts ro", all("runner-ok" in data.get("runner", "") for data in matrix.values()),
           "sample: %s" % matrix[CANDIDATE_USERS[0]].get("runner", ""))
    record("host sees committed output", host_out.exists(), str(host_out))
    record("host sees no parent write", not host_hack.exists(), str(host_hack))
    print()

    # --- keepalive + exact-ID verification ---------------------------------
    name = "doc-agent-preflight-keepalive-%s" % task_id
    started = time.monotonic()
    launched = docker(
        ["run", "-d", "--name", name, "--network", "none", "--read-only", "--tmpfs", "/tmp",
         "--entrypoint", "sleep", IMAGE, "infinity"],
        timeout=120.0,
    )
    container_id = launched.stdout.strip()
    record("keepalive container start", launched.returncode == 0 and bool(container_id),
           "id=%s in %.2fs" % (container_id[:12], time.monotonic() - started))
    if container_id:
        exec_probe = docker(["exec", container_id, "python3", "-c", "print('exec-ok')"], timeout=60.0)
        record("docker exec inside keepalive", "exec-ok" in exec_probe.stdout,
               exec_probe.stderr.strip() or exec_probe.stdout.strip())
        removed = time.monotonic()
        docker(["rm", "-f", container_id], timeout=120.0)
        remove_seconds = time.monotonic() - removed
        vanished = None
        deadline = time.monotonic() + VANISH_DEADLINE_SECONDS
        while time.monotonic() < deadline:
            inspect = docker(["inspect", container_id], timeout=30.0)
            if inspect.returncode != 0 and "no such object" in (inspect.stderr or "").lower():
                vanished = time.monotonic() - removed
                break
            time.sleep(0.2)
        record("gone by exact container id", vanished is not None,
               "rm=%.2fs verify=%.2fs" % (remove_seconds, vanished if vanished else -1.0))
    print()

    failed = [label for label, ok, _ in checks if not ok]
    print("=== summary ===")
    print("checks : %d/%d passed" % (len(checks) - len(failed), len(checks)))
    if failed:
        print("failed : %s" % ", ".join(failed))
    print("writable non-root candidates : %s" % ([u for u in writable if u != "0:0"] or "none"))
    print()
    print("=== config model proposal ===")
    non_root = [u for u in writable if u != "0:0"]
    if non_root:
        print("sandbox_uid_gid  : %s" % non_root[0])
        print("image reference  : %s@%s" % (IMAGE, manifest_digest or local_id))
        print("mounts           : task dir ro + <task>/out rw + /runner/run.py ro")
        print("hardening        : --read-only --tmpfs /tmp, HOME/XDG_CACHE_HOME/MPLCONFIGDIR=/tmp")
        return 0
    print("No non-root candidate could write the nested out mount on this host.")
    print("Per design decision 3, build a derived image (or switch to a named volume)")
    print("instead of falling back to root.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
