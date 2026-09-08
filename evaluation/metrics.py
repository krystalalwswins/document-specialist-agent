"""Aggregate persisted evidence. Missing provider token usage stays unknown."""
from task.task_model import TaskStatus


def summarize(tasks):
    completed = [t for t in tasks if t.status in (TaskStatus.SUCCESS, TaskStatus.FAILED)]
    required = [t for t in completed if t.artifact_requirements.get('required')]
    attempts = [e for t in tasks for e in t.metrics.get('retry_events', [])]
    usage = [e for t in tasks for e in t.metrics.get('llm_events', [])]
    durations = [t.metrics['duration_ms'] for t in completed if 'duration_ms' in t.metrics]
    return {
        'tasks': len(tasks), 'completed': len(completed),
        'success_rate': sum(t.status == TaskStatus.SUCCESS for t in completed) / len(completed) if completed else None,
        'artifact_pass_rate': sum(bool(t.metrics.get('artifacts')) for t in required) / len(required) if required else None,
        'tool_attempts': len(attempts),
        'retry_rate': sum(e.get('attempt', 1) > 1 for e in attempts) / len(attempts) if attempts else None,
        'mean_duration_ms': sum(durations) / len(durations) if durations else None,
        'llm_calls': len(usage),
        'known_total_tokens': sum(e.get('total_tokens') or 0 for e in usage),
        'usage_missing_calls': sum(e.get('total_tokens') is None for e in usage),
    }
