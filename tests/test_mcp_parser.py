"""Optional real MCP protocol roundtrip against a local child server."""
import json
import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

pytest.importorskip('mcp', reason='install requirements-mcp.txt for protocol integration')
from tools.mcp_document_tool import MCPDocumentTool


def test_real_mcp_http_parser_roundtrip(tmp_path):
    with socket.socket() as candidate:
        candidate.bind(('127.0.0.1', 0))
        port = candidate.getsockname()[1]
    env = dict(os.environ, MCP_PARSER_PORT=str(port))
    with (tmp_path / 'server.log').open('w+') as log:
        process = subprocess.Popen([sys.executable, '-m', 'demo.mcp_parser_server'], env=env,
                                   stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None or time.monotonic() > deadline:
                    log.seek(0)
                    pytest.fail('MCP server did not start: ' + log.read())
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            sandbox = SimpleNamespace(read_bytes_file=lambda filename: b'amount\n10\n20\n')
            tool = MCPDocumentTool(sandbox, f'http://127.0.0.1:{port}/mcp')
            result = json.loads(tool.execute('input.csv').output)
            assert result['columns'] == ['amount']
            assert result['row_count'] == 2
            missing = MCPDocumentTool(sandbox, f'http://127.0.0.1:{port}/mcp', 'missing_parser')
            with pytest.raises(Exception):
                missing.execute('input.csv')
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
