"""Optional MCP v1 Streamable HTTP adapter for the same parser contract."""
import asyncio
import base64
from datetime import timedelta
import json

from documents.files import safe_name, MAX_BYTES
from tools.document_tool import DocumentTool
from tools.base_tool import ToolResult


class MCPDocumentTool(DocumentTool):
    # Remote parsers are not automatically assumed safe to replay.
    retry_safe = False

    def __init__(self, sandbox, url, remote_tool='parse_document'):
        super().__init__(sandbox)
        self.url = url
        self.remote_tool = remote_tool

    async def _call(self, arguments):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        async with asyncio.timeout(30):
            async with streamablehttp_client(self.url) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=25)) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    if self.remote_tool not in {tool.name for tool in listing.tools}:
                        raise ValueError('configured parser tool not found on MCP server')
                    result = await session.call_tool(self.remote_tool, arguments)
                    if result.isError:
                        raise ValueError('MCP parser returned an error')
                    text = '\n'.join(item.text for item in result.content if item.type == 'text')
                    if len(text) > 50000:
                        raise ValueError('MCP parser result too large')
                    parsed = json.loads(text)
                    if not isinstance(parsed, dict) or 'format' not in parsed or 'preview' not in parsed:
                        raise ValueError('invalid MCP parser result contract')
                    return text

    def execute(self, filename):
        # No arbitrary server URLs or host paths are accepted from model arguments.
        data = self.sandbox.read_bytes_file(filename)
        if len(data) > MAX_BYTES:
            raise ValueError('document too large')
        arguments = {'filename': safe_name(filename),
                     'content_base64': base64.b64encode(data).decode('ascii')}
        return ToolResult(True, output=asyncio.run(self._call(arguments)))
