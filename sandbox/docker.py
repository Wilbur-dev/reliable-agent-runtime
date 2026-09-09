from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from runtime.models import ToolResult
from tools.base import ToolContext
from tools.errors import CommandTimeoutError


class DockerSandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str = "reliable-agent-runtime-sandbox:phase5"
    cpus: float = Field(default=1.0, gt=0)
    memory: str = Field(default="512m", pattern=r"^[1-9][0-9]*[kKmMgG]$")
    pids_limit: int = Field(default=128, gt=0)
    user: str = Field(default="65532:65532", pattern=r"^[0-9]+:[0-9]+$")
    tmpfs_size: str = Field(default="64m", pattern=r"^[1-9][0-9]*[kKmMgG]$")


class DockerCommandRunner:
    """Run an argv-only configured command in an ephemeral, restricted container."""

    def __init__(self, config: DockerSandboxConfig | None = None) -> None:
        self.config = config or DockerSandboxConfig()

    async def run(
        self,
        argv: Sequence[str],
        *,
        context: ToolContext,
        timeout_seconds: float,
        stdin: bytes | None = None,
        max_output_chars: int = 30_000,
    ) -> ToolResult:
        if stdin is not None:
            raise ValueError("Docker command runner does not accept stdin")
        task_label = re.sub(r"[^a-zA-Z0-9_.-]", "-", context.workspace.name)[:40]
        container_name = f"rar-{task_label}-{uuid.uuid4().hex[:10]}".lower()
        command = self.docker_argv(container_name, argv, context)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            await self._remove(container_name)
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
            metadata={
                "argv": list(argv),
                "sandbox": "docker",
                "container_name": container_name,
                "network": "none",
                "cpus": self.config.cpus,
                "memory": self.config.memory,
                "pids_limit": self.config.pids_limit,
                "user": self.config.user,
            },
        )

    def docker_argv(
        self, container_name: str, argv: Sequence[str], context: ToolContext
    ) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--label",
            "reliable-agent-runtime.phase=5",
            "--network",
            "none",
            "--cpus",
            str(self.config.cpus),
            "--memory",
            self.config.memory,
            "--pids-limit",
            str(self.config.pids_limit),
            "--user",
            self.config.user,
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={self.config.tmpfs_size}",
            "--mount",
            f"type=bind,src={context.workspace},dst=/workspace",
            "--workdir",
            "/workspace",
            self.config.image,
            *argv,
        ]

    @staticmethod
    async def _remove(container_name: str) -> None:
        cleanup = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await cleanup.wait()
