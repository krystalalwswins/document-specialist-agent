"""TaskWorkerPool: bounded, in-process execution for submitted tasks.

``POST /tasks`` used to spawn one unbounded daemon thread per request, so a
burst of submissions could thrash a single sandbox container. This pool keeps a
fixed number of workers and a bounded waiting queue; when the queue is full the
API rejects the submission (429) instead of silently piling up work.

Threads start lazily on the first submit so the pool also works in tests that do
not enter the FastAPI lifespan.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class QueueFull(Exception):
    """Raised when the pool has no room for another task."""


class TaskWorkerPool:
    def __init__(
        self,
        handler: Callable[[str], None],
        max_workers: int = 2,
        max_pending: int = 32,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if max_pending < 0:
            raise ValueError("max_pending must be non-negative")
        self._handler = handler
        self._max_workers = max_workers
        # Capacity counts running *and* waiting tasks, so the limit is
        # deterministic regardless of how fast workers pick items up.
        self._capacity = max_workers + max_pending
        self._queue: queue.Queue[str] = queue.Queue(maxsize=self._capacity)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._in_flight = 0
        self._running = 0
        self._completed = 0
        self._rejected = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._threads:
                return
            self._stop.clear()
            for index in range(self._max_workers):
                thread = threading.Thread(
                    target=self._loop, name=f"task-worker-{index}", daemon=True
                )
                thread.start()
                self._threads.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        """Ask workers to finish the current item and exit."""
        self._stop.set()
        with self._lock:
            threads, self._threads = self._threads, []
        for thread in threads:
            thread.join(timeout=timeout)

    # -- submission --------------------------------------------------------
    def submit(self, task_id: str) -> None:
        self.start()
        with self._lock:
            if self._in_flight >= self._capacity:
                self._rejected += 1
                raise QueueFull(
                    f"too many tasks in flight ({self._in_flight}/{self._capacity})"
                )
            self._in_flight += 1
        try:
            self._queue.put_nowait(task_id)
        except queue.Full:
            with self._lock:
                self._in_flight -= 1
                self._rejected += 1
            raise QueueFull(f"too many tasks in flight ({self._capacity} max)") from None

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "max_workers": self._max_workers,
                "capacity": self._capacity,
                "queued": self._queue.qsize(),
                "in_flight": self._in_flight,
                "running": self._running,
                "completed": self._completed,
                "rejected": self._rejected,
            }

    # -- worker loop -------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                task_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._lock:
                self._running += 1
            try:
                self._handler(task_id)
            except Exception:  # a failed task must not kill its worker
                logger.exception("task %s failed in worker", task_id)
            finally:
                with self._lock:
                    self._in_flight -= 1
                    self._running -= 1
                    self._completed += 1
                self._queue.task_done()
