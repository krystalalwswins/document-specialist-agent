"""A small durable queue: CREATED rows wait; workers atomically claim them."""
import logging
import threading

logger = logging.getLogger(__name__)


class TaskWorkers:
    def __init__(self, orchestrator, count=4):
        if count < 1:
            raise ValueError('worker count must be positive')
        self.orchestrator = orchestrator
        self.count = count
        self.stop_event = threading.Event()
        self.threads = []

    def run_once(self):
        task = self.orchestrator.task_manager.claim_next()
        if task is None:
            return False
        try:
            self.orchestrator.run_task(task.id, claimed=True)
        except Exception:
            logger.exception('worker task %s failed', task.id)
        return True

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                if self.run_once():
                    continue
            except Exception:
                logger.exception('queue polling failed')
            self.stop_event.wait(0.2)

    def start(self):
        for index in range(self.count):
            thread = threading.Thread(target=self._loop, name=f'agent-worker-{index}', daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stop_event.set()
        # Graceful shutdown waits for active tasks; this does not kill sandbox code.
        for thread in self.threads:
            thread.join()
