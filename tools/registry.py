from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from runtime.models import ToolResult
from tools.base import Tool, ToolContext
from tools.errors import ToolError


class DuplicateToolError(ValueError):
    pass


class UnknownToolError(LookupError):
    pass


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise DuplicateToolError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(f"Unknown tool: {name}") from exc

    def schemas(self) -> list[dict[str, object]]:
        return [self._tools[name].schema() for name in sorted(self._tools)]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        try:
            tool = self.get(name)
        except UnknownToolError as exc:
            return ToolResult(
                success=False,
                error_type="unknown_tool",
                error_message=str(exc),
            )

        try:
            validated = tool.args_model.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult(
                success=False,
                error_type="invalid_arguments",
                error_message=str(exc),
                metadata={"validation_errors": exc.errors(include_url=False)},
            )

        try:
            return await tool.execute(context, validated)
        except (OSError, ToolError, ValueError) as exc:
            return ToolResult(
                success=False,
                error_type=getattr(exc, "error_type", "tool_execution_error"),
                error_message=str(exc),
            )
