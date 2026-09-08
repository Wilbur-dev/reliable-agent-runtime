from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from llm.base import LLMClient
from llm.errors import LLMError
from runtime.actions import AgentAction, FinishAction, LLMUsage, ToolCallAction
from runtime.control import RunawayDetector
from runtime.models import Task, TaskStatus, TerminationReason, ToolResult
from tools.base import ToolContext
from tools.registry import ToolRegistry
from verifiers.base import VerificationResult, Verifier


class AgentStepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    action: AgentAction
    tool_result: ToolResult | None = None
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    model_latency_ms: float = Field(default=0.0, ge=0)
    model_metadata: dict[str, Any] = Field(default_factory=dict)


class RetryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1)
    error_type: str
    delay_seconds: float = Field(ge=0)


class AgentRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Task
    summary: str | None = None
    steps: list[AgentStepRecord]
    messages: list[dict[str, Any]]
    verifications: list[VerificationResult] = Field(default_factory=list)
    error: str | None = None
    elapsed_seconds: float = Field(default=0.0, ge=0)
    total_input_tokens: int = Field(default=0, ge=0)
    total_output_tokens: int = Field(default=0, ge=0)
    total_estimated_cost_usd: float = Field(default=0.0, ge=0)
    llm_retry_count: int = Field(default=0, ge=0)
    retry_events: list[RetryEvent] = Field(default_factory=list)


class InMemoryAgentLoop:
    def __init__(
        self,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        *,
        verifiers: list[Verifier] | None = None,
        tool_context: ToolContext | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.verifiers = verifiers or []
        self.tool_context = tool_context
        self.clock = clock
        self.sleep = sleep

    async def run(self, task: Task) -> AgentRunResult:
        task.status = TaskStatus.RUNNING
        context = self.tool_context or ToolContext(task.workspace)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "Work only inside the configured workspace. Use the provided tools. "
                    "A finish response requests verification; only the runtime can mark the task "
                    "complete. Do not modify protected paths. Never claim that an action happened "
                    "unless its tool result is present. When tests fail, inspect the relevant source "
                    "and use apply_patch. After verification fails, call tools to correct the failure "
                    "before requesting finish again. Return one real function call per turn; never "
                    "write XML, pseudo tool calls, or descriptions of actions you have not executed."
                ),
            },
            {
                "role": "user",
                "content": {
                    "goal": task.goal,
                    "constraints": task.constraints,
                    "acceptance_criteria": task.acceptance_criteria,
                },
            },
        ]
        steps: list[AgentStepRecord] = []
        verifications: list[VerificationResult] = []
        started_at = self.clock()
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        llm_retry_count = 0
        retry_events: list[RetryEvent] = []
        detector = RunawayDetector()

        def finish_result(
            *, summary: str | None = None, error: str | None = None
        ) -> AgentRunResult:
            return AgentRunResult(
                task=task,
                summary=summary,
                steps=steps,
                messages=messages,
                verifications=verifications,
                error=error,
                elapsed_seconds=max(0.0, self.clock() - started_at),
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_estimated_cost_usd=total_cost,
                llm_retry_count=llm_retry_count,
                retry_events=retry_events,
            )

        for sequence in range(1, task.budget.max_steps + 1):
            if self.clock() - started_at >= task.budget.max_wall_time:
                task.status = TaskStatus.BUDGET_EXCEEDED
                task.termination_reason = TerminationReason.MAX_WALL_TIME
                return finish_result()

            attempt = 0
            while True:
                try:
                    response = await self.llm_client.generate(
                        messages=messages,
                        tools=self.tool_registry.schemas(),
                    )
                    break
                except LLMError as exc:
                    if not exc.retryable or attempt >= task.budget.max_llm_retries:
                        task.status = TaskStatus.FAILED
                        task.termination_reason = TerminationReason.UNRECOVERABLE_ERROR
                        return finish_result(error=f"{exc.error_type}: {exc}")
                    delay = task.budget.retry_base_delay_seconds * (2**attempt)
                    attempt += 1
                    llm_retry_count += 1
                    retry_events.append(
                        RetryEvent(
                            attempt=attempt,
                            error_type=exc.error_type,
                            delay_seconds=delay,
                        )
                    )
                    if self.clock() - started_at + delay >= task.budget.max_wall_time:
                        task.status = TaskStatus.BUDGET_EXCEEDED
                        task.termination_reason = TerminationReason.MAX_WALL_TIME
                        return finish_result(error=f"{exc.error_type}: retry budget exhausted")
                    await self.sleep(delay)

            action = response.action
            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens
            total_cost += response.usage.estimated_cost_usd
            repeated_count = detector.observe_action(action)

            if repeated_count >= task.budget.max_repeated_actions:
                task.status = TaskStatus.FAILED
                task.termination_reason = TerminationReason.REPEATED_ACTIONS
                return finish_result(error="Repeated action threshold reached")

            if (
                task.budget.max_tokens is not None
                and total_input_tokens + total_output_tokens > task.budget.max_tokens
            ):
                task.status = TaskStatus.BUDGET_EXCEEDED
                task.termination_reason = TerminationReason.MAX_TOKENS
                return finish_result()
            if task.budget.max_cost_usd is not None and total_cost > task.budget.max_cost_usd:
                task.status = TaskStatus.BUDGET_EXCEEDED
                task.termination_reason = TerminationReason.MAX_COST
                return finish_result()
            if self.clock() - started_at >= task.budget.max_wall_time:
                task.status = TaskStatus.BUDGET_EXCEEDED
                task.termination_reason = TerminationReason.MAX_WALL_TIME
                return finish_result()

            if isinstance(action, FinishAction):
                steps.append(
                    AgentStepRecord(
                        sequence=sequence,
                        action=action,
                        llm_usage=response.usage,
                        model_latency_ms=response.latency_ms,
                        model_metadata=response.raw_metadata,
                    )
                )
                if not self.verifiers:
                    task.status = TaskStatus.COMPLETED
                    task.termination_reason = TerminationReason.MODEL_FINISH
                    return finish_result(summary=action.summary)

                current_verifications = [
                    await verifier.verify(task=task, context=context, steps=steps)
                    for verifier in self.verifiers
                ]
                verifications.extend(current_verifications)
                if all(result.success for result in current_verifications):
                    task.status = TaskStatus.COMPLETED
                    task.termination_reason = TerminationReason.VERIFIED_COMPLETE
                    return finish_result(summary=action.summary)

                messages.extend(
                    [
                        {"role": "assistant", "content": action.model_dump(mode="json")},
                        {
                            "role": "tool",
                            "name": "verification",
                            "content": {
                                "success": False,
                                "results": [
                                    result.model_dump(mode="json")
                                    for result in current_verifications
                                ],
                                "instruction": "Verification failed. Continue working and fix the evidence.",
                            },
                        },
                    ]
                )
                continue

            if not isinstance(action, ToolCallAction):
                raise TypeError(f"Unsupported action: {type(action).__name__}")

            tool_calls = sum(isinstance(step.action, ToolCallAction) for step in steps)
            if tool_calls >= task.budget.max_tool_calls:
                task.status = TaskStatus.BUDGET_EXCEEDED
                task.termination_reason = TerminationReason.MAX_TOOL_CALLS
                break

            result = await self.tool_registry.execute(
                action.tool_name,
                action.arguments,
                context,
            )
            steps.append(
                AgentStepRecord(
                    sequence=sequence,
                    action=action,
                    tool_result=result,
                    llm_usage=response.usage,
                    model_latency_ms=response.latency_ms,
                    model_metadata=response.raw_metadata,
                )
            )
            consecutive_failures, no_progress_steps = detector.observe_result(action, result)
            messages.extend(
                [
                    {"role": "assistant", "content": action.model_dump(mode="json")},
                    {
                        "role": "tool",
                        "name": action.tool_name,
                        "content": result.model_dump(mode="json"),
                    },
                ]
            )
            if self.clock() - started_at >= task.budget.max_wall_time:
                task.status = TaskStatus.BUDGET_EXCEEDED
                task.termination_reason = TerminationReason.MAX_WALL_TIME
                return finish_result()
            if consecutive_failures >= task.budget.max_consecutive_failures:
                task.status = TaskStatus.FAILED
                task.termination_reason = TerminationReason.CONSECUTIVE_FAILURES
                return finish_result(error="Consecutive tool failure threshold reached")
            if no_progress_steps >= task.budget.max_no_progress_steps:
                task.status = TaskStatus.FAILED
                task.termination_reason = TerminationReason.NO_PROGRESS
                return finish_result(error="No-progress threshold reached")
        else:
            task.status = TaskStatus.BUDGET_EXCEEDED
            task.termination_reason = TerminationReason.MAX_STEPS

        return finish_result()
