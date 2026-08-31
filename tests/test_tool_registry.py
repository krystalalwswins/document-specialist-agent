"""ToolRegistry + BaseTool tests."""

import pytest

from tools.base_tool import BaseTool, ToolError, ToolResult
from tools.tool_registry import ToolRegistry


class EchoTool(BaseTool):
    name = "echo"
    description = "echo text back"

    def parameters_schema(self):
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    def execute(self, text):
        return ToolResult(success=True, output=text)


class FailingTool(BaseTool):
    name = "fail"
    description = "always fails"

    def parameters_schema(self):
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        return ToolResult(success=False, error="boom")


def test_register_get_and_names():
    registry = ToolRegistry()
    registry.register(EchoTool())
    assert registry.names() == ["echo"]
    assert registry.get("echo").name == "echo"


def test_unknown_tool_raises():
    registry = ToolRegistry()
    with pytest.raises(ToolError, match="unknown tool"):
        registry.get("nope")


def test_to_openai_tools_schema_shape():
    registry = ToolRegistry()
    registry.register(EchoTool())
    schema = registry.to_openai_tools()
    assert schema[0]["type"] == "function"
    assert schema[0]["function"]["name"] == "echo"
    assert schema[0]["function"]["parameters"]["required"] == ["text"]


def test_execute_returns_result():
    registry = ToolRegistry()
    registry.register(EchoTool())
    result = registry.execute("echo", {"text": "hi"})
    assert result.success is True
    assert result.to_text() == "hi"


def test_failing_tool_returns_error_text():
    registry = ToolRegistry()
    registry.register(FailingTool())
    result = registry.execute("fail", {})
    assert result.success is False
    assert "[tool error] boom" in result.to_text()
