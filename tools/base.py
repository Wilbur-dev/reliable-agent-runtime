from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from runtime.models import ToolResult


class ToolContext:
    def __init__(
        self,
        workspace: str | Path,
        *,
        protected_paths: Sequence[str] = ("tests",),
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError(f"Workspace is not a directory: {self.workspace}")
        self.protected_paths = tuple(Path(path).as_posix().rstrip("/") for path in protected_paths)

    def resolve_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            raise ValueError("Absolute paths are not allowed")

        resolved = (self.workspace / candidate).resolve(strict=False)
        if not resolved.is_relative_to(self.workspace):
            raise ValueError("Path escapes the workspace")
        return resolved

    def is_protected(self, relative_path: str | Path) -> bool:
        normalized = Path(relative_path).as_posix().lstrip("./")
        return any(
            normalized == protected or normalized.startswith(f"{protected}/")
            for protected in self.protected_paths
        )


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[BaseModel]]
    mutates_environment: ClassVar[bool] = False
    risk_level: ClassVar[str] = "low"

    def schema(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.args_model.model_json_schema(),
        }

    @abstractmethod
    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        """Execute a validated tool call."""
