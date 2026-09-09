from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from runtime.models import ToolResult
from tools.base import Tool, ToolContext
from tools.errors import (
    CommandNotAllowedError,
    CommandTimeoutError,
    PatchApplyError,
    ProtectedPathError,
)


class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApplyPatchArgs(StrictArgs):
    patch: str = Field(min_length=1, max_length=200_000)


class GitDiffArgs(StrictArgs):
    staged: bool = False


class CommandArgs(StrictArgs):
    command_name: str = Field(min_length=1)


async def _run_process(
    argv: Sequence[str],
    *,
    context: ToolContext,
    timeout_seconds: float,
    stdin: bytes | None = None,
    max_output_chars: int = 30_000,
) -> ToolResult:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=context.workspace,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout_seconds)
    except TimeoutError as exc:
        process.kill()
        stdout, stderr = await process.communicate()
        raise CommandTimeoutError(f"Command timed out after {timeout_seconds:g}s") from exc

    rendered = b"".join([stdout, stderr]).decode("utf-8", errors="replace")
    truncated = len(rendered) > max_output_chars
    if truncated:
        rendered = rendered[:max_output_chars] + "\n... [output truncated]"
    return ToolResult(
        success=process.returncode == 0,
        output=rendered,
        exit_code=process.returncode,
        truncated=truncated,
        metadata={"argv": list(argv)},
    )


def _patch_paths(patch: str) -> set[str]:
    paths: set[str] = set()
    for match in re.finditer(r"^(?:---|\+\+\+)\s+([^\t\n]+)", patch, re.MULTILINE):
        raw_path = match.group(1)
        if raw_path == "/dev/null":
            continue
        if raw_path.startswith(("a/", "b/")):
            raw_path = raw_path[2:]
        paths.add(raw_path)
    if not paths:
        raise PatchApplyError("Patch does not contain unified diff file headers")
    return paths


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = "Apply a unified diff inside the workspace, excluding protected paths."
    args_model = ApplyPatchArgs
    mutates_environment = True
    risk_level = "medium"

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = ApplyPatchArgs.model_validate(arguments)
        paths = _patch_paths(args.patch)
        for path in paths:
            context.resolve_path(path)
            if context.is_protected(path):
                raise ProtectedPathError(f"Patch targets protected path: {path}")

        check = await _run_process(
            ("git", "apply", "--check", "--whitespace=error", "-"),
            context=context,
            timeout_seconds=10,
            stdin=args.patch.encode(),
        )
        if not check.success:
            raise PatchApplyError(check.output or "Patch cannot be applied")

        result = await _run_process(
            ("git", "apply", "--whitespace=error", "-"),
            context=context,
            timeout_seconds=10,
            stdin=args.patch.encode(),
        )
        if not result.success:
            raise PatchApplyError(result.output or "Patch could not be applied")
        result.metadata["changed_paths"] = sorted(paths)
        return result


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Show the current workspace git diff."
    args_model = GitDiffArgs

    def __init__(self, *, max_output_chars: int = 30_000) -> None:
        self.max_output_chars = max_output_chars

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = GitDiffArgs.model_validate(arguments)
        argv = ["git", "diff"]
        if args.staged:
            argv.append("--cached")
        return await _run_process(
            argv,
            context=context,
            timeout_seconds=10,
            max_output_chars=self.max_output_chars,
        )


class ConfiguredCommandTool(Tool):
    args_model = CommandArgs

    def __init__(
        self,
        commands: Mapping[str, Sequence[str]],
        *,
        timeout_seconds: float = 60,
        max_output_chars: int = 30_000,
    ) -> None:
        self.commands = {name: tuple(argv) for name, argv in commands.items()}
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    def schema(self) -> dict[str, object]:
        schema = super().schema()
        parameters = schema["parameters"]
        if isinstance(parameters, dict):
            properties = parameters.get("properties")
            if isinstance(properties, dict) and isinstance(properties.get("command_name"), dict):
                properties["command_name"]["enum"] = sorted(self.commands)
        allowed = ", ".join(sorted(self.commands))
        schema["description"] = f"{self.description} Allowed names: {allowed}."
        return schema

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        args = CommandArgs.model_validate(arguments)
        argv = self.commands.get(args.command_name)
        if argv is None:
            raise CommandNotAllowedError(f"Command is not configured: {args.command_name}")
        if context.command_runner is not None:
            return await context.command_runner.run(
                argv,
                context=context,
                timeout_seconds=self.timeout_seconds,
                max_output_chars=self.max_output_chars,
            )
        return await _run_process(
            argv,
            context=context,
            timeout_seconds=self.timeout_seconds,
            max_output_chars=self.max_output_chars,
        )


class RunTestsTool(ConfiguredCommandTool):
    name = "run_tests"
    description = (
        "Run a configured test command by name; arbitrary shell commands are not accepted."
    )
    risk_level = "medium"


class RunLinterTool(ConfiguredCommandTool):
    name = "run_linter"
    description = (
        "Run a configured linter command by name; arbitrary shell commands are not accepted."
    )
    risk_level = "medium"
