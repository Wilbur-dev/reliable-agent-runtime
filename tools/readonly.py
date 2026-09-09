from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from runtime.models import ToolResult
from tools.base import Tool, ToolContext
from tools.errors import FileEncodingError, FileTooLargeError, PathBoundaryError


class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadOnlyLimits(StrictArgs):
    max_depth: int = Field(default=6, ge=0, le=20)
    max_file_bytes: int = Field(default=256_000, gt=0)
    max_output_chars: int = Field(default=20_000, gt=0)
    max_search_results: int = Field(default=100, gt=0)


class ListFilesArgs(StrictArgs):
    path: str = "."
    max_depth: int | None = Field(default=None, ge=0, le=20)


class ReadFileArgs(StrictArgs):
    path: str
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, gt=0, le=2_000)


class SearchTextArgs(StrictArgs):
    query: str = Field(min_length=1)
    path: str = "."
    max_results: int | None = Field(default=None, gt=0, le=1_000)


def _is_hidden(relative_path: Path) -> bool:
    return any(part.startswith(".") for part in relative_path.parts)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    marker = "\n... [output truncated]"
    if max_chars <= len(marker):
        return marker[:max_chars], True
    return text[: max(0, max_chars - len(marker))] + marker, True


class ReadOnlyTool(Tool):
    def __init__(self, limits: ReadOnlyLimits | None = None) -> None:
        self.limits = limits or ReadOnlyLimits()

    def _safe_path(self, context: ToolContext, raw_path: str) -> Path:
        try:
            return context.resolve_path(raw_path)
        except ValueError as exc:
            raise PathBoundaryError(str(exc)) from exc

    def _read_text(self, path: Path) -> str:
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path.name}")
        size = path.stat().st_size
        if size > self.limits.max_file_bytes:
            raise FileTooLargeError(
                f"File exceeds {self.limits.max_file_bytes} byte limit: {size} bytes"
            )
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise FileEncodingError("File is not valid UTF-8 text") from exc


class ListFilesTool(ReadOnlyTool):
    name = "list_files"
    description = "List visible files inside the task workspace."
    args_model = ListFilesArgs

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = ListFilesArgs.model_validate(arguments)
        root = self._safe_path(context, args.path)
        if not root.is_dir():
            raise NotADirectoryError(f"Directory not found: {args.path}")

        max_depth = args.max_depth if args.max_depth is not None else self.limits.max_depth
        entries: list[str] = []
        for path in sorted(root.rglob("*")):
            relative_to_root = path.relative_to(root)
            if _is_hidden(relative_to_root) or len(relative_to_root.parts) > max_depth:
                continue
            relative_to_workspace = path.relative_to(context.workspace).as_posix()
            entries.append(relative_to_workspace + ("/" if path.is_dir() else ""))

        output, truncated = _truncate("\n".join(entries), self.limits.max_output_chars)
        return ToolResult(
            success=True,
            output=output,
            truncated=truncated,
            metadata={"entry_count": len(entries), "path": args.path},
        )


class ReadFileTool(ReadOnlyTool):
    name = "read_file"
    description = "Read a line range from a UTF-8 text file in the task workspace."
    args_model = ReadFileArgs

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = ReadFileArgs.model_validate(arguments)
        path = self._safe_path(context, args.path)
        lines = self._read_text(path).splitlines()
        start_index = args.start_line - 1
        selected = lines[start_index : start_index + args.max_lines]
        rendered = "\n".join(
            f"{line_number}: {line}"
            for line_number, line in enumerate(selected, start=args.start_line)
        )
        output, truncated = _truncate(rendered, self.limits.max_output_chars)
        return ToolResult(
            success=True,
            output=output,
            truncated=truncated,
            metadata={
                "path": args.path,
                "start_line": args.start_line,
                "line_count": len(selected),
                "total_lines": len(lines),
                "content_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
            },
        )


class SearchTextTool(ReadOnlyTool):
    name = "search_text"
    description = "Search visible UTF-8 text files inside the task workspace."
    args_model = SearchTextArgs

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = SearchTextArgs.model_validate(arguments)
        root = self._safe_path(context, args.path)
        if not root.exists():
            raise FileNotFoundError(f"Path not found: {args.path}")

        result_limit = args.max_results or self.limits.max_search_results
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        matches: list[str] = []
        skipped_files = 0
        result_limit_hit = False

        for path in candidates:
            if not path.is_file():
                continue
            relative = path.relative_to(context.workspace)
            if _is_hidden(relative):
                continue
            try:
                content = self._read_text(path)
            except (FileEncodingError, FileTooLargeError, OSError):
                skipped_files += 1
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                if args.query in line:
                    matches.append(f"{relative.as_posix()}:{line_number}:{line}")
                    if len(matches) >= result_limit:
                        result_limit_hit = True
                        break
            if result_limit_hit:
                break

        rendered, output_truncated = _truncate("\n".join(matches), self.limits.max_output_chars)
        return ToolResult(
            success=True,
            output=rendered,
            truncated=result_limit_hit or output_truncated,
            metadata={
                "match_count": len(matches),
                "skipped_files": skipped_files,
                "query": args.query,
            },
        )
