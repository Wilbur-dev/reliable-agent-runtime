from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from llm.base import LLMClient
from llm.errors import LLMError
from policies.engine import PolicyDecision, PolicyEngine
from runtime.actions import AgentAction, FinishAction, LLMUsage, ToolCallAction
from runtime.context import ContextBuilder
from runtime.control import RunawayDetector
from runtime.models import Task, TaskStatus, TerminationReason, ToolResult
from runtime.observability import JsonEventLogger
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
    tool_latency_ms: float = Field(default=0.0, ge=0)
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
    context_compaction_count: int = Field(default=0, ge=0)
    prompt_tokens_before_compaction: int = Field(default=0, ge=0)
    prompt_tokens_after_compaction: int = Field(default=0, ge=0)


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
        checkpoint_store: Any | None = None,
        resume_messages: list[dict[str, Any]] | None = None,
        sequence_offset: int = 0,
        initial_metrics: dict[str, Any] | None = None,
        initial_tool_call_count: int = 0,
        history_steps: list[AgentStepRecord] | None = None,
        policy_engine: PolicyEngine | None = None,
        context_builder: ContextBuilder | None = None,
        event_logger: JsonEventLogger | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.verifiers = verifiers or []
        self.tool_context = tool_context
        self.clock = clock
        self.sleep = sleep
        self.checkpoint_store = checkpoint_store
        self.resume_messages = resume_messages
        self.sequence_offset = sequence_offset
        self.initial_metrics = initial_metrics or {}
        self.initial_tool_call_count = initial_tool_call_count
        self.history_steps = history_steps or []
        self.policy_engine = policy_engine
        self.context_builder = context_builder
        self.event_logger = event_logger or JsonEventLogger()

    async def run(self, task: Task) -> AgentRunResult:
        task.status = TaskStatus.RUNNING
        context = self.tool_context or ToolContext(task.workspace)
        messages: list[dict[str, Any]] = self.resume_messages or [
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
        total_input_tokens = int(self.initial_metrics.get("input_tokens", 0))
        total_output_tokens = int(self.initial_metrics.get("output_tokens", 0))
        total_cost = float(self.initial_metrics.get("estimated_cost_usd", 0.0))
        llm_retry_count = int(self.initial_metrics.get("llm_retry_count", 0))
        context_compaction_count = int(self.initial_metrics.get("context_compaction_count", 0))
        prompt_tokens_before = int(self.initial_metrics.get("prompt_tokens_before_compaction", 0))
        prompt_tokens_after = int(self.initial_metrics.get("prompt_tokens_after_compaction", 0))
        retry_events: list[RetryEvent] = []
        detector = RunawayDetector()
        prior_elapsed = float(self.initial_metrics.get("elapsed_seconds", 0.0))

        def elapsed() -> float:
            return prior_elapsed + max(0.0, self.clock() - started_at)

        if self.checkpoint_store is not None:
            self.checkpoint_store.set_task_status(str(task.id), TaskStatus.RUNNING)
            self.checkpoint_store.replace_messages(str(task.id), messages)

        def finish_result(
            *, summary: str | None = None, error: str | None = None
        ) -> AgentRunResult:
            result = AgentRunResult(
                task=task,
                summary=summary,
                steps=steps,
                messages=messages,
                verifications=verifications,
                error=error,
                elapsed_seconds=elapsed(),
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_estimated_cost_usd=total_cost,
                llm_retry_count=llm_retry_count,
                retry_events=retry_events,
                context_compaction_count=context_compaction_count,
                prompt_tokens_before_compaction=prompt_tokens_before,
                prompt_tokens_after_compaction=prompt_tokens_after,
            )
            if self.checkpoint_store is not None:
                self.checkpoint_store.replace_messages(str(task.id), messages)
                self.checkpoint_store.set_task_status(
                    str(task.id),
                    task.status,
                    termination_reason=(
                        task.termination_reason.value if task.termination_reason else None
                    ),
                    metrics={
                        "elapsed_seconds": result.elapsed_seconds,
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                        "estimated_cost_usd": total_cost,
                        "llm_retry_count": llm_retry_count,
                        "context_compaction_count": context_compaction_count,
                        "prompt_tokens_before_compaction": prompt_tokens_before,
                        "prompt_tokens_after_compaction": prompt_tokens_after,
                    },
                )
                self.checkpoint_store.record_event(
                    str(task.id),
                    "task_finished",
                    {
                        "status": task.status.value,
                        "termination_reason": (
                            task.termination_reason.value if task.termination_reason else None
                        ),
                        "elapsed_seconds": result.elapsed_seconds,
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                        "estimated_cost_usd": total_cost,
                        "context_compaction_count": context_compaction_count,
                    },
                )
            self.event_logger.emit(
                "task_finished",
                task_id=str(task.id),
                status=task.status.value,
                termination_reason=(
                    task.termination_reason.value if task.termination_reason else None
                ),
            )
            return result

        start_sequence = self.sequence_offset + 1
        for sequence in range(start_sequence, task.budget.max_steps + 1):
            if self.checkpoint_store is not None and self.checkpoint_store.is_cancelled(
                str(task.id)
            ):
                task.status = TaskStatus.CANCELLED
                task.termination_reason = TerminationReason.CANCELLED
                return finish_result()
            if elapsed() >= task.budget.max_wall_time:
                task.status = TaskStatus.BUDGET_EXCEEDED
                task.termination_reason = TerminationReason.MAX_WALL_TIME
                return finish_result()

            attempt = 0
            while True:
                try:
                    model_messages = messages
                    if self.context_builder is not None:
                        context_view = self.context_builder.build(
                            messages, workspace=task.workspace
                        )
                        model_messages = context_view.messages
                        prompt_tokens_before = context_view.estimated_tokens_before
                        prompt_tokens_after = context_view.estimated_tokens_after
                        if context_view.compacted:
                            context_compaction_count += 1
                            summary_message = next(
                                (
                                    message
                                    for message in model_messages
                                    if message.get("name") == "context_summary"
                                ),
                                {"content": {}},
                            )
                            if self.checkpoint_store is not None:
                                self.checkpoint_store.save_context_compaction(
                                    str(task.id),
                                    sequence,
                                    chars_before=context_view.chars_before,
                                    chars_after=context_view.chars_after,
                                    tokens_before=context_view.estimated_tokens_before,
                                    tokens_after=context_view.estimated_tokens_after,
                                    compacted_message_count=(context_view.compacted_message_count),
                                    summary=summary_message["content"],
                                )
                                self.checkpoint_store.record_event(
                                    str(task.id),
                                    "context_compacted",
                                    {
                                        "tokens_before": context_view.estimated_tokens_before,
                                        "tokens_after": context_view.estimated_tokens_after,
                                        "compacted_message_count": (
                                            context_view.compacted_message_count
                                        ),
                                        "result_ids": context_view.result_ids,
                                        "stale_file_references": (
                                            context_view.stale_file_references
                                        ),
                                    },
                                    step_sequence=sequence,
                                )
                            self.event_logger.emit(
                                "context_compacted",
                                task_id=str(task.id),
                                step_sequence=sequence,
                                tokens_before=context_view.estimated_tokens_before,
                                tokens_after=context_view.estimated_tokens_after,
                            )
                    response = await self.llm_client.generate(
                        messages=model_messages,
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
                    self.event_logger.emit(
                        "model_retry",
                        task_id=str(task.id),
                        step_sequence=sequence,
                        attempt=attempt,
                        error_type=exc.error_type,
                        delay_seconds=delay,
                    )
                    if self.checkpoint_store is not None:
                        self.checkpoint_store.record_event(
                            str(task.id),
                            "model_retry",
                            {
                                "attempt": attempt,
                                "error_type": exc.error_type,
                                "delay_seconds": delay,
                            },
                            step_sequence=sequence,
                        )
                    if elapsed() + delay >= task.budget.max_wall_time:
                        task.status = TaskStatus.BUDGET_EXCEEDED
                        task.termination_reason = TerminationReason.MAX_WALL_TIME
                        return finish_result(error=f"{exc.error_type}: retry budget exhausted")
                    await self.sleep(delay)

            action = response.action
            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens
            total_cost += response.usage.estimated_cost_usd
            if self.checkpoint_store is not None:
                self.checkpoint_store.record_event(
                    str(task.id),
                    "model_finished",
                    {
                        "model_latency_ms": response.latency_ms,
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                        "estimated_cost_usd": response.usage.estimated_cost_usd,
                    },
                    step_sequence=sequence,
                )
            self.event_logger.emit(
                "model_finished",
                task_id=str(task.id),
                step_sequence=sequence,
                model_latency_ms=response.latency_ms,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                estimated_cost_usd=response.usage.estimated_cost_usd,
            )
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
            if elapsed() >= task.budget.max_wall_time:
                task.status = TaskStatus.BUDGET_EXCEEDED
                task.termination_reason = TerminationReason.MAX_WALL_TIME
                return finish_result()

            if isinstance(action, FinishAction):
                checkpoint_step_id = None
                if self.checkpoint_store is not None:
                    checkpoint_step_id = self.checkpoint_store.begin_step(
                        task, sequence, action, mutates=False
                    )
                steps.append(
                    AgentStepRecord(
                        sequence=sequence,
                        action=action,
                        llm_usage=response.usage,
                        model_latency_ms=response.latency_ms,
                        model_metadata=response.raw_metadata,
                    )
                )
                if checkpoint_step_id is not None:
                    self.checkpoint_store.finish_step(
                        checkpoint_step_id, None, task=task, mutates=False
                    )
                if not self.verifiers:
                    task.status = TaskStatus.COMPLETED
                    task.termination_reason = TerminationReason.MODEL_FINISH
                    return finish_result(summary=action.summary)

                current_verifications = [
                    await verifier.verify(
                        task=task, context=context, steps=[*self.history_steps, *steps]
                    )
                    for verifier in self.verifiers
                ]
                verifications.extend(current_verifications)
                if self.checkpoint_store is not None:
                    self.checkpoint_store.save_verifications(
                        str(task.id),
                        sequence,
                        [result.model_dump(mode="json") for result in current_verifications],
                    )
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
                if self.checkpoint_store is not None:
                    self.checkpoint_store.replace_messages(str(task.id), messages)
                continue

            if not isinstance(action, ToolCallAction):
                raise TypeError(f"Unsupported action: {type(action).__name__}")

            tool_calls = self.initial_tool_call_count + sum(
                isinstance(step.action, ToolCallAction) for step in steps
            )
            if tool_calls >= task.budget.max_tool_calls:
                task.status = TaskStatus.BUDGET_EXCEEDED
                task.termination_reason = TerminationReason.MAX_TOOL_CALLS
                break

            policy_result = None
            if self.policy_engine is not None:
                try:
                    policy_tool = self.tool_registry.get(action.tool_name)
                except LookupError:
                    policy_tool = None
                policy_result = self.policy_engine.evaluate(
                    action,
                    tool=policy_tool,
                    context=context,
                    task_constraints=task.constraints,
                )
                if self.checkpoint_store is not None:
                    self.checkpoint_store.record_event(
                        str(task.id),
                        "policy_decision",
                        {
                            "decision": policy_result.decision.value,
                            "rule": policy_result.rule,
                            "tool_name": action.tool_name,
                        },
                        step_sequence=sequence,
                    )
                self.event_logger.emit(
                    "policy_decision",
                    task_id=str(task.id),
                    step_sequence=sequence,
                    decision=policy_result.decision.value,
                    rule=policy_result.rule,
                    tool_name=action.tool_name,
                )
                if policy_result.decision is PolicyDecision.REQUIRE_APPROVAL:
                    approved = bool(
                        self.checkpoint_store
                        and self.checkpoint_store.consume_approval(
                            str(task.id), policy_result.action_fingerprint
                        )
                    )
                    if not approved:
                        if self.checkpoint_store is None:
                            task.status = TaskStatus.FAILED
                            task.termination_reason = TerminationReason.UNRECOVERABLE_ERROR
                            return finish_result(
                                error="Approval-required actions need a checkpoint store"
                            )
                        self.checkpoint_store.request_approval(
                            task,
                            sequence,
                            action,
                            action_fingerprint=policy_result.action_fingerprint,
                            rule=policy_result.rule,
                            reason=policy_result.reason,
                        )
                        task.status = TaskStatus.WAITING_APPROVAL
                        return finish_result(summary=policy_result.reason)

            # A PENDING and then RUNNING row is committed before any tool side effect.
            checkpoint_step_id = None
            mutates = False
            if self.checkpoint_store is not None:
                try:
                    mutates = self.tool_registry.get(action.tool_name).mutates_environment
                except LookupError:
                    mutates = False
                checkpoint_step_id = self.checkpoint_store.begin_step(
                    task, sequence, action, mutates=mutates
                )
            tool_started_at = self.clock()
            if policy_result and policy_result.decision is PolicyDecision.DENY:
                result = ToolResult(
                    success=False,
                    error_type="policy_denied",
                    error_message=policy_result.reason,
                    metadata={"policy_rule": policy_result.rule},
                )
            else:
                result = await self.tool_registry.execute(
                    action.tool_name,
                    action.arguments,
                    context,
                )
            tool_latency_ms = max(0.0, self.clock() - tool_started_at) * 1000
            steps.append(
                AgentStepRecord(
                    sequence=sequence,
                    action=action,
                    tool_result=result,
                    llm_usage=response.usage,
                    model_latency_ms=response.latency_ms,
                    tool_latency_ms=tool_latency_ms,
                    model_metadata=response.raw_metadata,
                )
            )
            if checkpoint_step_id is not None:
                self.checkpoint_store.finish_step(
                    checkpoint_step_id, result, task=task, mutates=mutates
                )
                self.checkpoint_store.record_event(
                    str(task.id),
                    "tool_finished",
                    {
                        "tool_name": action.tool_name,
                        "success": result.success,
                        "error_type": result.error_type,
                        "exit_code": result.exit_code,
                        "tool_latency_ms": tool_latency_ms,
                    },
                    step_sequence=sequence,
                )
            self.event_logger.emit(
                "tool_finished",
                task_id=str(task.id),
                step_sequence=sequence,
                tool_name=action.tool_name,
                success=result.success,
                error_type=result.error_type,
                exit_code=result.exit_code,
                tool_latency_ms=tool_latency_ms,
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
            if self.checkpoint_store is not None:
                self.checkpoint_store.replace_messages(str(task.id), messages)
            if elapsed() >= task.budget.max_wall_time:
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
