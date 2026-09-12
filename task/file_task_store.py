"""FileTaskStore: one JSON document per task, so a restart does not lose history.

Design notes:

- the store implements the same ``TaskStore`` contract as ``InMemoryTaskStore``,
  so ``TaskManager`` and every caller stay unchanged;
- writes are atomic (temp file + ``os.replace``), so a crash mid-write cannot
  leave a half-written task record;
- the directory is created lazily on the first write, keeping construction (and
  therefore importing this module) free of filesystem side effects;
- this is a single-process store: ``TaskManager`` already serializes
  get→mutate→update, and the lock here protects the file itself. Running several
  uvicorn workers against one directory is out of scope (use Redis for that).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from threading import RLock
from typing import Union

from .task_manager import TaskStore
from .task_model import Task, TaskError, TaskNotFoundError

logger = logging.getLogger(__name__)

# Task ids are uuid4 hex; rejecting anything else also blocks path traversal.
_SAFE_ID = re.compile(r"\A[0-9a-f]{32}\Z")


class FileTaskStore(TaskStore):
    def __init__(self, directory: Union[str, Path]) -> None:
        self._dir = Path(directory)
        self._lock = RLock()

    @property
    def directory(self) -> Path:
        return self._dir

    def _path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not _SAFE_ID.match(task_id):
            raise TaskNotFoundError(str(task_id))
        return self._dir / f"{task_id}.json"

    def create(self, task: Task) -> None:
        path = self._path(task.id)
        with self._lock:
            if path.exists():
                raise TaskError(f"task already exists: {task.id}")
            self._write(task)

    def get(self, task_id: str) -> Task:
        path = self._path(task_id)
        with self._lock:
            if not path.exists():
                raise TaskNotFoundError(task_id)
            return self._read(path, task_id)

    def list(self) -> list[Task]:
        with self._lock:
            if not self._dir.is_dir():
                return []
            tasks: list[Task] = []
            for path in sorted(self._dir.glob("*.json")):
                task_id = path.stem
                try:
                    tasks.append(self._read(path, task_id))
                except TaskNotFoundError:
                    logger.warning("skipping unreadable task record: %s", path)
            tasks.sort(key=lambda task: task.created_time)
            return tasks

    def update(self, task: Task) -> None:
        path = self._path(task.id)
        with self._lock:
            if not path.exists():
                raise TaskNotFoundError(task.id)
            self._write(task)

    def _write(self, task: Task) -> None:
        path = self._path(task.id)
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(task.to_dict(), ensure_ascii=False, indent=2)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)  # atomic on the same filesystem

    @staticmethod
    def _read(path: Path, task_id: str) -> Task:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return Task.from_dict(raw)
        except FileNotFoundError:
            raise TaskNotFoundError(task_id) from None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TaskNotFoundError(f"{task_id} (unreadable record: {exc})") from exc
