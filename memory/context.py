"""Bound visible messages by characters, preserving complete tool-call groups."""
import copy
import json


class ContextBudgetExceeded(ValueError):
    pass


def compact_messages(messages, max_chars=24000):
    result = copy.deepcopy(messages)
    def size():
        return len(json.dumps(result, ensure_ascii=False))
    # First two messages contain the original instructions and current task.
    while size() > max_chars and len(result) > 2:
        end = 3
        if result[2].get('tool_calls'):
            expected = {call['id'] for call in result[2]['tool_calls']}
            seen = set()
            while end < len(result) and result[end]['role'] == 'tool':
                seen.add(result[end]['tool_call_id'])
                end += 1
            if expected != seen:
                raise ContextBudgetExceeded('incomplete tool-call group')
        # Never drop the newest group; it is needed to interpret the last result.
        if end == len(result):
            break
        del result[2:end]
    if size() > max_chars:
        raise ContextBudgetExceeded('context budget exceeded; use smaller inputs or tool results')
    return result


def result_reference(text, task_id, step_id, limit=2000):
    if len(text) <= limit:
        return text
    return (text[:limit] + f'\n[truncated; full output: task={task_id}, step={step_id}; '
            'use read_step_output with offset/limit]')
