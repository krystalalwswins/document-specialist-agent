"""Host-side Docker CLI access for the one-shot execution backend.

Only the trusted host process talks to Docker. Nothing in this module is
reachable from inside a sandbox container, and no container ever receives the
Docker socket (design note 14, security invariant 1).

`DockerRunner` is the seam that lets the whole execution path be tested offline:
production uses `SubprocessDockerRunner`, tests inject `FakeDockerRunner`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence


class DockerTimeout(RuntimeError):
    """The host-side timer expired while waiting for a Docker command."""


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class DockerRunner(Protocol):
    def run(self, args: Sequence[str], *, timeout: float) -> CommandResult:
        ...


def find_docker_cli() -> Optional[str]:
    """Locate the Docker CLI without assuming it is on PATH."""
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


class SubprocessDockerRunner:
    """Real Docker CLI. `timeout` is the host-side timer, not docker's own."""

    def __init__(self, docker_path: Optional[str] = None) -> None:
        # Resolution is lazy so that composing the stack stays Docker-free: the
        # error only surfaces when something actually tries to run a container.
        self._docker_path = docker_path or find_docker_cli()

    @property
    def docker_path(self) -> str:
        return self._docker_path or ""

    def run(self, args: Sequence[str], *, timeout: float) -> CommandResult:
        if not self._docker_path:
            raise DockerTimeout("docker command is unavailable on this host")
        argv = [self._docker_path, *args]
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise DockerTimeout(
                "docker command exceeded %.1fs: %s" % (timeout, " ".join(args))
            ) from exc
        return CommandResult(tuple(args), completed.returncode, completed.stdout, completed.stderr)


@dataclass
class FakeContainer:
    container_id: str
    name: str = ""
    argv: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    removed: bool = False
    exec_calls: list[tuple[str, ...]] = field(default_factory=list)


class FakeDockerRunner:
    """In-memory Docker CLI for offline tests.

    Implements exactly the command shapes the supervisor and sweeper emit:
    ``image inspect``, ``run -d``, ``exec``, ``rm -f``, ``inspect`` and ``ps``.
    Tests script behaviour through the attributes below instead of touching a
    real engine.
    """

    def __init__(
        self,
        *,
        image_present: bool = True,
        container_prefix: str = "fake-cid",
    ) -> None:
        self.image_present = image_present
        self.container_prefix = container_prefix
        self.calls: list[tuple[str, ...]] = []
        self.containers: dict[str, FakeContainer] = {}
        self.exec_queue: list[CommandResult] = []
        self.exec_default = CommandResult((), 0, "", "")
        self.timeout_markers: set[str] = set()
        self.rm_fail_ids: set[str] = set()
        self.sticky_ids: set[str] = set()
        self.run_failure: Optional[CommandResult] = None
        self._counter = 0

    # -- test helpers -------------------------------------------------------
    def queue_exec(self, stdout: str = "", *, returncode: int = 0, stderr: str = "") -> None:
        self.exec_queue.append(CommandResult((), returncode, stdout, stderr))

    def fail_removal(self, container_id: str) -> None:
        self.rm_fail_ids.add(container_id)

    def stick_container(self, container_id: str) -> None:
        """Simulate a container that survives `rm -f` (verification must fail)."""
        self.sticky_ids.add(container_id)

    def next_container_id(self) -> str:
        self._counter += 1
        return "%s-%d" % (self.container_prefix, self._counter)

    # -- DockerRunner -------------------------------------------------------
    def run(self, args: Sequence[str], *, timeout: float) -> CommandResult:
        argv = tuple(args)
        self.calls.append(argv)
        for marker in self.timeout_markers:
            if marker in argv:
                raise DockerTimeout("fake timeout on %r" % (marker,))

        head = argv[0] if argv else ""
        if head == "image":
            return self._image_inspect(argv)
        if head == "run":
            return self._run(argv)
        if head == "exec":
            return self._exec(argv)
        if head == "rm":
            return self._rm(argv)
        if head == "inspect":
            return self._inspect(argv)
        if head == "ps":
            return self._ps(argv)
        return CommandResult(argv, 127, "", "unknown fake docker command: %s" % head)

    # -- command handlers ---------------------------------------------------
    def _image_inspect(self, argv: tuple[str, ...]) -> CommandResult:
        if not self.image_present:
            return CommandResult(argv, 1, "", "Error: No such image")
        return CommandResult(argv, 0, "sha256:fake-image-id\n", "")

    def _run(self, argv: tuple[str, ...]) -> CommandResult:
        if self.run_failure is not None:
            return self.run_failure
        labels: dict[str, str] = {}
        name = ""
        index = 1
        while index < len(argv):
            token = argv[index]
            if token == "--label" and index + 1 < len(argv):
                key, _, value = argv[index + 1].partition("=")
                labels[key] = value
                index += 2
                continue
            if token == "--name" and index + 1 < len(argv):
                name = argv[index + 1]
                index += 2
                continue
            index += 1
        container_id = self.next_container_id()
        self.containers[container_id] = FakeContainer(
            container_id=container_id, name=name, argv=list(argv), labels=labels
        )
        return CommandResult(argv, 0, container_id + "\n", "")

    def _exec(self, argv: tuple[str, ...]) -> CommandResult:
        container_id = argv[1] if len(argv) > 1 else ""
        container = self.containers.get(container_id)
        rest = argv[2:]
        if container is not None:
            container.exec_calls.append(rest)
        if self.exec_queue:
            queued = self.exec_queue.pop(0)
            return CommandResult(argv, queued.returncode, queued.stdout, queued.stderr)
        return CommandResult(argv, self.exec_default.returncode, self.exec_default.stdout, self.exec_default.stderr)

    def _rm(self, argv: tuple[str, ...]) -> CommandResult:
        container_id = argv[-1] if len(argv) > 1 else ""
        if container_id in self.rm_fail_ids:
            return CommandResult(argv, 1, "", "Error response from daemon: cannot remove")
        container = self.containers.get(container_id)
        if container is not None and container_id not in self.sticky_ids:
            container.removed = True
        return CommandResult(argv, 0, container_id + "\n", "")

    def _inspect(self, argv: tuple[str, ...]) -> CommandResult:
        container_id = argv[1] if len(argv) > 1 else ""
        container = self.containers.get(container_id)
        if container is None or container.removed:
            return CommandResult(argv, 1, "", "error: no such object: %s" % container_id)
        if "--format" in argv and any("Config.Labels" in token for token in argv):
            return CommandResult(argv, 0, json.dumps(container.labels) + "\n", "")
        return CommandResult(argv, 0, "[{}]\n", "")

    def _ps(self, argv: tuple[str, ...]) -> CommandResult:
        label_filter = ""
        for index, token in enumerate(argv):
            if token == "--filter" and index + 1 < len(argv) and argv[index + 1].startswith("label="):
                label_filter = argv[index + 1][len("label=") :]
        key, _, value = label_filter.partition("=")
        ids = []
        for container in self.containers.values():
            if container.removed:
                continue
            if key and container.labels.get(key) != value:
                continue
            ids.append(container.container_id)
        return CommandResult(argv, 0, "".join(item + "\n" for item in ids), "")


RunnerFactory = Callable[[], DockerRunner]
