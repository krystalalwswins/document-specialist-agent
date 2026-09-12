"""TaskWorkerPool: bounded concurrency, bounded queue, lifecycle."""

import threading
import time

import pytest

from api.workers import QueueFull, TaskWorkerPool


def test_runs_tasks_and_reports_stats():
    done = []
    pool = TaskWorkerPool(done.append, max_workers=2, max_pending=4)

    pool.submit("a")
    pool.submit("b")
    deadline = time.monotonic() + 5
    while len(done) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    pool.stop()

    assert sorted(done) == ["a", "b"]
    assert pool.stats()["completed"] == 2
    assert pool.stats()["running"] == 0


def test_concurrency_is_bounded_by_max_workers():
    peak = {"value": 0, "now": 0}
    lock = threading.Lock()
    release = threading.Event()

    def handler(_task_id):
        with lock:
            peak["now"] += 1
            peak["value"] = max(peak["value"], peak["now"])
        release.wait(timeout=5)
        with lock:
            peak["now"] -= 1

    pool = TaskWorkerPool(handler, max_workers=2, max_pending=10)
    for index in range(6):
        pool.submit(f"task-{index}")
    time.sleep(0.3)
    observed = peak["value"]
    release.set()
    pool.stop()

    assert observed == 2


def test_queue_full_is_rejected_and_counted():
    release = threading.Event()
    pool = TaskWorkerPool(lambda _task_id: release.wait(timeout=5), max_workers=1, max_pending=0)

    pool.submit("first")  # picked up by the worker and blocked in the handler
    time.sleep(0.1)

    with pytest.raises(QueueFull):
        pool.submit("second")

    release.set()
    pool.stop()
    assert pool.stats()["rejected"] == 1


def test_handler_failure_does_not_kill_the_worker():
    seen = []

    def handler(task_id):
        seen.append(task_id)
        if task_id == "boom":
            raise RuntimeError("task failed")

    pool = TaskWorkerPool(handler, max_workers=1, max_pending=5)
    pool.submit("boom")
    pool.submit("after")
    deadline = time.monotonic() + 5
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    pool.stop()

    assert seen == ["boom", "after"]
    assert pool.stats()["completed"] == 2


def test_invalid_sizes_are_rejected():
    with pytest.raises(ValueError):
        TaskWorkerPool(lambda _t: None, max_workers=0)
    with pytest.raises(ValueError):
        TaskWorkerPool(lambda _t: None, max_pending=-1)
