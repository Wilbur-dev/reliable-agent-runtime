from __future__ import annotations

import asyncio

from runtime.models import Task, ToolResult
from tools.base import ToolContext
from verifiers.base import VerificationResult, Verifier


def _tool_observations(steps: list[object]) -> list[tuple[str, ToolResult]]:
    observations: list[tuple[str, ToolResult]] = []
    for step in steps:
        action = getattr(step, "action", None)
        result = getattr(step, "tool_result", None)
        tool_name = getattr(action, "tool_name", None)
        if tool_name and isinstance(result, ToolResult):
            observations.append((tool_name, result))
    return observations


class CommandExitCodeVerifier(Verifier):
    name = "command_exit_code"

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name

    async def verify(
        self,
        *,
        task: Task,
        context: ToolContext,
        steps: list[object],
    ) -> VerificationResult:
        del task, context
        results = [result for name, result in _tool_observations(steps) if name == self.tool_name]
        if not results:
            return VerificationResult(
                verifier=self.name,
                success=False,
                evidence=f"No {self.tool_name} result is available.",
                metadata={"tool_name": self.tool_name},
            )
        result = results[-1]
        success = result.success and result.exit_code == 0
        evidence = (
            f"Latest {self.tool_name} exited with code {result.exit_code}."
            if success
            else f"Latest {self.tool_name} failed with code {result.exit_code}:\n{result.output or result.error_message or ''}"
        )
        return VerificationResult(
            verifier=self.name,
            success=success,
            evidence=evidence,
            metadata={"tool_name": self.tool_name, "exit_code": result.exit_code},
        )


class ProtectedPathsUnchangedVerifier(Verifier):
    name = "protected_paths_unchanged"

    async def verify(
        self,
        *,
        task: Task,
        context: ToolContext,
        steps: list[object],
    ) -> VerificationResult:
        del task, steps
        process = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            cwd=context.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            return VerificationResult(
                verifier=self.name,
                success=False,
                evidence=f"Unable to inspect changed paths: {error}",
            )
        changed_paths = []
        for line in output.splitlines():
            if not line:
                continue
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", maxsplit=1)[1]
            changed_paths.append(path)
        protected_changes = [path for path in changed_paths if context.is_protected(path)]
        return VerificationResult(
            verifier=self.name,
            success=not protected_changes,
            evidence=(
                "Protected paths are unchanged."
                if not protected_changes
                else f"Protected paths changed: {', '.join(protected_changes)}"
            ),
            metadata={"changed_paths": changed_paths, "protected_changes": protected_changes},
        )
