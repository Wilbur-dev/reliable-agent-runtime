from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from llm.base import LLMClient
from runtime.actions import AgentAction, FinishAction, ToolCallAction
from runtime.models import Task, TaskStatus, TerminationReason, ToolResult
from tools.base import ToolContext
from tools.registry import ToolRegistry


class AgentStepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    action: AgentAction
    tool_result: ToolResult | None = None


class AgentRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Task
    summary: str | None = None
    steps: list[AgentStepRecord]
    messages: list[dict[str, Any]]


class InMemoryAgentLoop:
    def __init__(self, llm_client: LLMClient, tool_registry: ToolRegistry) -> None:
        self.llm_client = llm_client
        self.tool_registry = tool_registry

    async def run(self, task: Task) -> AgentRunResult:
        task.status = TaskStatus.RUNNING
        context = ToolContext(task.workspace)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "Work only inside the configured workspace. Use the provided tools and return "
                    "finish when the analysis is complete."
                ),
            },
            {"role": "user", "content": task.goal},
        ]
        steps: list[AgentStepRecord] = []

        for sequence in range(1, task.budget.max_steps + 1):
            response = await self.llm_client.generate(
                messages=messages,
                tools=self.tool_registry.schemas(),
            )
            action = response.action

            if isinstance(action, FinishAction):
                steps.append(AgentStepRecord(sequence=sequence, action=action))
                task.status = TaskStatus.COMPLETED
                task.termination_reason = TerminationReason.MODEL_FINISH
                return AgentRunResult(
                    task=task,
                    summary=action.summary,
                    steps=steps,
                    messages=messages,
                )

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
            steps.append(AgentStepRecord(sequence=sequence, action=action, tool_result=result))
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

        return AgentRunResult(task=task, steps=steps, messages=messages)
