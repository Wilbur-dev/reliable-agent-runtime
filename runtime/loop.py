from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from llm.base import LLMClient
from llm.errors import LLMError
from runtime.actions import AgentAction, FinishAction, LLMUsage, ToolCallAction
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


class AgentRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Task
    summary: str | None = None
    steps: list[AgentStepRecord]
    messages: list[dict[str, Any]]
    verifications: list[VerificationResult] = Field(default_factory=list)
    error: str | None = None


class InMemoryAgentLoop:
    def __init__(
        self,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        *,
        verifiers: list[Verifier] | None = None,
        tool_context: ToolContext | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.verifiers = verifiers or []
        self.tool_context = tool_context

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

        for sequence in range(1, task.budget.max_steps + 1):
            try:
                response = await self.llm_client.generate(
                    messages=messages,
                    tools=self.tool_registry.schemas(),
                )
            except LLMError as exc:
                task.status = TaskStatus.FAILED
                task.termination_reason = TerminationReason.UNRECOVERABLE_ERROR
                return AgentRunResult(
                    task=task,
                    steps=steps,
                    messages=messages,
                    verifications=verifications,
                    error=f"{exc.error_type}: {exc}",
                )
            action = response.action

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
                    return AgentRunResult(
                        task=task,
                        summary=action.summary,
                        steps=steps,
                        messages=messages,
                    )

                current_verifications = [
                    await verifier.verify(task=task, context=context, steps=steps)
                    for verifier in self.verifiers
                ]
                verifications.extend(current_verifications)
                if all(result.success for result in current_verifications):
                    task.status = TaskStatus.COMPLETED
                    task.termination_reason = TerminationReason.VERIFIED_COMPLETE
                    return AgentRunResult(
                        task=task,
                        summary=action.summary,
                        steps=steps,
                        messages=messages,
                        verifications=verifications,
                    )

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
        else:
            task.status = TaskStatus.BUDGET_EXCEEDED
            task.termination_reason = TerminationReason.MAX_STEPS

        return AgentRunResult(
            task=task,
            steps=steps,
            messages=messages,
            verifications=verifications,
        )
