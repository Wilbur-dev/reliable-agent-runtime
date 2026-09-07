import asyncio

import pytest

from tools.base import ToolContext
from tools.readonly import ListFilesTool
from tools.registry import DuplicateToolError, ToolRegistry


def test_registry_rejects_duplicate_names() -> None:
    with pytest.raises(DuplicateToolError):
        ToolRegistry([ListFilesTool(), ListFilesTool()])


def test_registry_exports_tool_schemas() -> None:
    registry = ToolRegistry([ListFilesTool()])

    assert registry.schemas()[0]["name"] == "list_files"
    assert "properties" in registry.schemas()[0]["parameters"]


def test_unknown_tool_returns_structured_failure(tmp_path) -> None:
    result = asyncio.run(ToolRegistry().execute("missing", {}, ToolContext(tmp_path)))

    assert result.success is False
    assert result.error_type == "unknown_tool"


def test_invalid_arguments_do_not_execute_tool(tmp_path) -> None:
    registry = ToolRegistry([ListFilesTool()])

    result = asyncio.run(
        registry.execute("list_files", {"path": ".", "unexpected": True}, ToolContext(tmp_path))
    )

    assert result.success is False
    assert result.error_type == "invalid_arguments"
