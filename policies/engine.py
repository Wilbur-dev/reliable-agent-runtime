from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from pathlib import PurePosixPath
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from runtime.actions import ToolCallAction
from tools.base import Tool, ToolContext


class PolicyDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class PolicyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: PolicyDecision
    reason: str
    rule: str
    action_fingerprint: str


def policy_action_fingerprint(action: ToolCallAction) -> str:
    payload = json.dumps(action.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class PolicyEngine:
    """Deterministic guardrails evaluated before a tool is executed."""

    dependency_files: ClassVar[frozenset[str]] = frozenset(
        {
            "dockerfile",
            "pyproject.toml",
            "requirements.txt",
            "requirements-dev.txt",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "poetry.lock",
            "uv.lock",
        }
    )

    def __init__(self, *, large_change_lines: int = 200, large_change_files: int = 5) -> None:
        self.large_change_lines = large_change_lines
        self.large_change_files = large_change_files

    def evaluate(
        self,
        action: ToolCallAction,
        *,
        tool: Tool | None,
        context: ToolContext,
        task_constraints: list[str] | None = None,
    ) -> PolicyResult:
        fingerprint = policy_action_fingerprint(action)

        if tool is None:
            return PolicyResult(
                decision=PolicyDecision.DENY,
                reason=f"Unknown tool: {action.tool_name}",
                rule="unknown_tool",
                action_fingerprint=fingerprint,
            )

        if action.tool_name == "apply_patch":
            patch = str(action.arguments.get("patch", ""))
            paths = self._patch_paths(patch)
            if any(context.is_protected(path) for path in paths):
                return PolicyResult(
                    decision=PolicyDecision.DENY,
                    reason="Patch targets a protected path",
                    rule="protected_path",
                    action_fingerprint=fingerprint,
                )
            if any(PurePosixPath(path).name.lower() in self.dependency_files for path in paths):
                return PolicyResult(
                    decision=PolicyDecision.REQUIRE_APPROVAL,
                    reason="Dependency or container configuration changes require approval",
                    rule="dependency_change",
                    action_fingerprint=fingerprint,
                )
            if re.search(r"^deleted file mode |^\+\+\+ /dev/null$", patch, re.MULTILINE):
                return PolicyResult(
                    decision=PolicyDecision.REQUIRE_APPROVAL,
                    reason="File deletion requires approval",
                    rule="file_deletion",
                    action_fingerprint=fingerprint,
                )
            changed_lines = sum(
                1
                for line in patch.splitlines()
                if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
            )
            if len(paths) >= self.large_change_files or changed_lines >= self.large_change_lines:
                return PolicyResult(
                    decision=PolicyDecision.REQUIRE_APPROVAL,
                    reason="Large code change requires approval",
                    rule="large_change",
                    action_fingerprint=fingerprint,
                )

        if tool.risk_level == "high" or action.tool_name in {
            "network_request",
            "install_dependency",
        }:
            return PolicyResult(
                decision=PolicyDecision.REQUIRE_APPROVAL,
                reason="High-risk or network-capable action requires approval",
                rule="high_risk_action",
                action_fingerprint=fingerprint,
            )

        return PolicyResult(
            decision=PolicyDecision.ALLOW,
            reason="Action is permitted by configured policy",
            rule="default_allow",
            action_fingerprint=fingerprint,
        )

    @staticmethod
    def _patch_paths(patch: str) -> set[str]:
        paths: set[str] = set()
        for raw in re.findall(r"^(?:---|\+\+\+)\s+([^\t\n]+)", patch, re.MULTILINE):
            if raw == "/dev/null":
                continue
            paths.add(raw[2:] if raw.startswith(("a/", "b/")) else raw)
        return paths
