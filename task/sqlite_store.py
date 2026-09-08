"""SQLite 存储：一行保存一个任务快照，事务保护读—修改—写。"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from threading import local

from .task_manager import TaskStore
from .task_model import Task, TaskError, TaskNotFoundError


class SQLiteTaskStore(TaskStore):
    def __init__(self, path: str):
        if path == ':memory:':
            raise ValueError('use InMemoryTaskStore for memory storage')
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = local()
        with self.transaction():
            self._local.connection.execute(
                'CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL)'
            )

    @contextmanager
    def transaction(self):
        # 同一线程内嵌套操作共用连接，整个生命周期修改只提交一次。
        if getattr(self._local, 'connection', None) is not None:
            yield
            return
        connection = sqlite3.connect(self.path, timeout=30)
        self._local.connection = connection
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            self._local.connection = None
            connection.close()

    def create(self, task: Task) -> None:
        with self.transaction():
            try:
                self._local.connection.execute(
                    'INSERT INTO tasks VALUES (?, ?)',
                    (task.id, json.dumps(task.to_dict(), ensure_ascii=False)),
                )
            except sqlite3.IntegrityError as exc:
                raise TaskError(f'task already exists: {task.id}') from exc

    def get(self, task_id: str) -> Task:
        with self.transaction():
            row = self._local.connection.execute(
                'SELECT data FROM tasks WHERE id = ?', (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(task_id)
            return Task.from_dict(json.loads(row[0]))

    def list(self) -> list[Task]:
        with self.transaction():
            rows = self._local.connection.execute('SELECT data FROM tasks ORDER BY rowid').fetchall()
            return [Task.from_dict(json.loads(row[0])) for row in rows]

    def update(self, task: Task) -> None:
        with self.transaction():
            cursor = self._local.connection.execute(
                'UPDATE tasks SET data = ? WHERE id = ?',
                (json.dumps(task.to_dict(), ensure_ascii=False), task.id),
            )
            if cursor.rowcount != 1:
                raise TaskNotFoundError(task.id)
