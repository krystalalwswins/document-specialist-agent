"""Parse local sandbox document bytes; MCP is optional and explicitly configured."""
import json
from documents.files import inspect_document
from tools.base_tool import BaseTool, ToolResult


class DocumentTool(BaseTool):
    name = 'parse_document'
    description = 'Parse CSV/XLSX/text; return headers, row count and a small preview.'
    required_permissions = frozenset({'file.read'})
    file_parameters = ('filename',)
    retry_safe = True

    def __init__(self, sandbox):
        self.sandbox = sandbox

    def parameters_schema(self):
        return {'type': 'object', 'additionalProperties': False,
                'properties': {'filename': {'type': 'string'}}, 'required': ['filename']}

    def execute(self, filename):
        result = inspect_document(filename, self.sandbox.read_bytes_file(filename), min_rows=0)
        return ToolResult(True, output=json.dumps(result, ensure_ascii=False, default=str))
