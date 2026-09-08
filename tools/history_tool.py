"""Read a bounded slice of this task's persisted tool output."""
from agent.runtime import current_run
from tools.base_tool import BaseTool, ToolResult, ErrorType


class HistoryTool(BaseTool):
    name = 'read_step_output'
    description = 'Read a slice of a previous tool output from this task only.'
    retry_safe = True

    def parameters_schema(self):
        return {'type': 'object', 'additionalProperties': False, 'properties': {
            'step_id': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 2000}}, 'required': ['step_id']}

    def execute(self, step_id, offset=0, limit=2000):
        context = current_run.get()
        if context is None:
            return ToolResult(False, error='no active task', error_type=ErrorType.PERMISSION_DENIED)
        step = context.manager.get_task(context.task_id).get_step(step_id)
        return ToolResult(True, output=(step.output or step.error or '')[offset:offset + limit])
