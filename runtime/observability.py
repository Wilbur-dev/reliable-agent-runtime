from __future__ import annotations

import json
import logging
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest


class JsonEventLogger:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("reliable_agent.runtime")

    def emit(
        self,
        event_type: str,
        *,
        task_id: str,
        step_sequence: int | None = None,
        **payload: Any,
    ) -> None:
        event = {
            "event_type": event_type,
            "task_id": task_id,
            "step_sequence": step_sequence,
            **payload,
        }
        self.logger.info(json.dumps(event, sort_keys=True, separators=(",", ":")))


class RuntimeMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.tasks = Counter(
            "agent_tasks",
            "Agent task lifecycle events",
            ("event", "status"),
            registry=self.registry,
        )
        self.duration = Histogram(
            "agent_task_duration_seconds",
            "End-to-end task run duration",
            registry=self.registry,
        )
        self.tool_calls = Counter(
            "agent_tool_calls",
            "Tool calls by tool and outcome",
            ("tool", "success"),
            registry=self.registry,
        )
        self.tool_errors = Counter(
            "agent_tool_errors",
            "Tool errors by tool and type",
            ("tool", "error_type"),
            registry=self.registry,
        )
        self.llm_tokens = Counter(
            "agent_llm_tokens",
            "Model tokens by direction",
            ("direction",),
            registry=self.registry,
        )
        self.budget_exceeded = Counter(
            "agent_budget_exceeded",
            "Tasks stopped by budget reason",
            ("reason",),
            registry=self.registry,
        )
        self.verification_failures = Counter(
            "agent_verification_failures",
            "Failed verifier results",
            ("verifier",),
            registry=self.registry,
        )
        self.context_compactions = Counter(
            "agent_context_compactions",
            "Model context compaction operations",
            registry=self.registry,
        )

    def observe_task_created(self) -> None:
        self.tasks.labels(event="created", status="PENDING").inc()

    def observe_run(self, result: Any, *, initial_metrics: dict[str, Any] | None = None) -> None:
        initial = initial_metrics or {}
        status = result.task.status.value
        self.tasks.labels(event="finished", status=status).inc()
        self.duration.observe(
            max(0.0, result.elapsed_seconds - float(initial.get("elapsed_seconds", 0.0)))
        )
        self.llm_tokens.labels(direction="input").inc(
            max(0, result.total_input_tokens - int(initial.get("input_tokens", 0)))
        )
        self.llm_tokens.labels(direction="output").inc(
            max(0, result.total_output_tokens - int(initial.get("output_tokens", 0)))
        )
        for step in result.steps:
            tool_name = getattr(step.action, "tool_name", None)
            if tool_name is None or step.tool_result is None:
                continue
            success = str(step.tool_result.success).lower()
            self.tool_calls.labels(tool=tool_name, success=success).inc()
            if not step.tool_result.success:
                self.tool_errors.labels(
                    tool=tool_name,
                    error_type=step.tool_result.error_type or "unknown",
                ).inc()
        for verification in result.verifications:
            if not verification.success:
                self.verification_failures.labels(verifier=verification.verifier).inc()
        if status == "BUDGET_EXCEEDED":
            reason = result.task.termination_reason.value if result.task.termination_reason else ""
            self.budget_exceeded.labels(reason=reason).inc()
        self.context_compactions.inc(
            max(
                0,
                result.context_compaction_count - int(initial.get("context_compaction_count", 0)),
            )
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)
