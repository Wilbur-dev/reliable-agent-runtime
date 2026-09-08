from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from runtime.actions import AgentAction, ToolCallAction
from runtime.models import ToolResult


def action_fingerprint(action: AgentAction) -> str:
    """Return a stable fingerprint for an action and normalized arguments."""
    payload = action.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def result_fingerprint(result: ToolResult) -> str:
    payload: dict[str, Any] = {
        "success": result.success,
        "output": result.output,
        "error_type": result.error_type,
        "error_message": result.error_message,
        "exit_code": result.exit_code,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


class RunawayDetector:
    def __init__(self) -> None:
        self.action_counts: Counter[str] = Counter()
        self.consecutive_failure_key: tuple[str | None, int | None] | None = None
        self.consecutive_failures = 0
        self.no_progress_steps = 0
        self._last_test_result: str | None = None

    def observe_action(self, action: AgentAction) -> int:
        fingerprint = action_fingerprint(action)
        self.action_counts[fingerprint] += 1
        return self.action_counts[fingerprint]

    def observe_result(self, action: ToolCallAction, result: ToolResult) -> tuple[int, int]:
        if result.success:
            self.consecutive_failure_key = None
            self.consecutive_failures = 0
        else:
            failure_key = (result.error_type, result.exit_code)
            if failure_key == self.consecutive_failure_key:
                self.consecutive_failures += 1
            else:
                self.consecutive_failure_key = failure_key
                self.consecutive_failures = 1

        made_progress = False
        changed_paths = result.metadata.get("changed_paths")
        if result.success and changed_paths:
            made_progress = True
        elif action.tool_name in {"run_tests", "run_linter"}:
            fingerprint = result_fingerprint(result)
            made_progress = fingerprint != self._last_test_result
            self._last_test_result = fingerprint

        self.no_progress_steps = 0 if made_progress else self.no_progress_steps + 1
        return self.consecutive_failures, self.no_progress_steps
