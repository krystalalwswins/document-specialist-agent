"""Run only after stopping ALL API/worker processes. Does not stop sandbox code."""
import argparse
from task.sqlite_store import SQLiteTaskStore
from task.task_manager import TaskManager


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='data/tasks.sqlite3')
    parser.add_argument('--workers-stopped', action='store_true', required=True,
                        help='acknowledge that all workers have been stopped')
    args = parser.parse_args()
    manager = TaskManager(SQLiteTaskStore(args.db))
    print({'marked_failed': manager.recover_interrupted(),
           'reason': 'worker_interrupted_execution_uncertain; no tasks replayed'})


if __name__ == '__main__':
    main()
