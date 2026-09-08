"""Optional local MCP server: pip install -r requirements-mcp.txt first."""
import json
from mcp.server.fastmcp import FastMCP
from documents.files import decode_inputs, inspect_document

server = FastMCP('document-parser', host='127.0.0.1', port=8001)


@server.tool()
def parse_document(filename: str, content_base64: str) -> str:
    """Read supplied bytes only; never accept a host file path."""
    name, data = decode_inputs([{'filename': filename, 'content_base64': content_base64}])[0]
    return json.dumps(inspect_document(name, data, min_rows=0), ensure_ascii=False, default=str)


if __name__ == '__main__':
    server.run(transport='streamable-http')
