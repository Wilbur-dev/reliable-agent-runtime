import asyncio
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from llm.base import LLMClient
from llm.errors import LLMAuthenticationError, LLMTimeoutError
from llm.fake import FakeLLMClient
from runtime.actions import FinishAction, LLMResponse, LLMUsage, ToolCallAction
from runtime.control import action_fingerprint
from runtime.loop import InMemoryAgentLoop
from runtime.models import BudgetConfig, Task, TaskStatus, TerminationReason
from tools.base import ToolContext
from tools.readonly import ListFilesTool, ReadFileTool, SearchTextTool
from tools.registry import ToolRegistry


def run_loop(
    client: LLMClient,
    tmp_path: Path,
    *,
    budget: BudgetConfig,
    registry: ToolRegistry | None = None,
    clock=None,
    sleep=None,
):
    kwargs: dict[str, Any] = {"tool_context": ToolContext(tmp_path)}
    if clock is not None:
        kwargs["clock"] = clock
    if sleep is not None:
        kwargs["sleep"] = sleep
    loop = InMemoryAgentLoop(client, registry or ToolRegistry(), **kwargs)
    return asyncio.run(
        loop.run(Task(goal="exercise controls", workspace=str(tmp_path), budget=budget))
    )


@pytest.mark.parametrize(
    ("budget", "usage", "reason"),
    [
        (
            BudgetConfig(max_tokens=9),
            LLMUsage(input_tokens=8, output_tokens=2),
            TerminationReason.MAX_TOKENS,
        ),
        (
            BudgetConfig(max_cost_usd=0.01),
            LLMUsage(estimated_cost_usd=0.02),
            TerminationReason.MAX_COST,
        ),
    ],
)
def test_token_and_cost_budgets_stop_before_action(tmp_path, budget, usage, reason) -> None:
    client = FakeLLMClient([LLMResponse(action=FinishAction(summary="done"), usage=usage)])

    result = run_loop(client, tmp_path, budget=budget)

    assert result.task.status is TaskStatus.BUDGET_EXCEEDED
    assert result.task.termination_reason is reason
    assert result.total_input_tokens == usage.input_tokens
    assert result.total_output_tokens == usage.output_tokens
    assert result.total_estimated_cost_usd == usage.estimated_cost_usd
    assert result.steps == []


class SequenceClient(LLMClient):
    def __init__(self, outcomes) -> None:
        self.outcomes = deque(outcomes)
        self.call_count = 0

    async def generate(self, *, messages, tools=None) -> LLMResponse:
        del messages, tools
        self.call_count += 1
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_retryable_model_errors_use_bounded_exponential_backoff(tmp_path) -> None:
    client = SequenceClient(
        [
            LLMTimeoutError("one"),
            LLMTimeoutError("two"),
            LLMResponse(action=FinishAction(summary="recovered")),
        ]
    )
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    result = run_loop(
        client,
        tmp_path,
        budget=BudgetConfig(max_llm_retries=2, retry_base_delay_seconds=0.1),
        sleep=record_sleep,
    )

    assert result.task.status is TaskStatus.COMPLETED
    assert result.llm_retry_count == 2
    assert client.call_count == 3
    assert delays == pytest.approx([0.1, 0.2])
    assert [event.attempt for event in result.retry_events] == [1, 2]
    assert {event.error_type for event in result.retry_events} == {"llm_timeout_error"}


def test_permanent_model_error_is_not_retried(tmp_path) -> None:
    client = SequenceClient([LLMAuthenticationError("bad credentials")])

    result = run_loop(client, tmp_path, budget=BudgetConfig(max_llm_retries=5))

    assert result.task.status is TaskStatus.FAILED
    assert result.task.termination_reason is TerminationReason.UNRECOVERABLE_ERROR
    assert result.llm_retry_count == 0
    assert client.call_count == 1


def test_retryable_model_error_stops_after_retry_limit(tmp_path) -> None:
    client = SequenceClient([LLMTimeoutError("one"), LLMTimeoutError("two")])

    result = run_loop(
        client,
        tmp_path,
        budget=BudgetConfig(max_llm_retries=1, retry_base_delay_seconds=0),
    )

    assert result.task.status is TaskStatus.FAILED
    assert result.task.termination_reason is TerminationReason.UNRECOVERABLE_ERROR
    assert result.llm_retry_count == 1
    assert client.call_count == 2


def test_action_fingerprint_normalizes_argument_order() -> None:
    first = ToolCallAction(tool_name="search_text", arguments={"path": ".", "query": "x"})
    second = ToolCallAction(tool_name="search_text", arguments={"query": "x", "path": "."})

    assert action_fingerprint(first) == action_fingerprint(second)


def test_repeated_action_loop_is_stopped(tmp_path) -> None:
    action = ToolCallAction(tool_name="list_files", arguments={"path": "."})
    client = FakeLLMClient([action, action, action, action])

    result = run_loop(
        client,
        tmp_path,
        budget=BudgetConfig(max_repeated_actions=3, max_no_progress_steps=10),
        registry=ToolRegistry([ListFilesTool()]),
    )

    assert result.task.status is TaskStatus.FAILED
    assert result.task.termination_reason is TerminationReason.REPEATED_ACTIONS
    assert len(result.steps) == 2


def test_consecutive_same_class_failures_trip_circuit_breaker(tmp_path) -> None:
    actions = [
        ToolCallAction(tool_name="missing", arguments={"attempt": attempt}) for attempt in range(3)
    ]

    result = run_loop(
        FakeLLMClient(actions),
        tmp_path,
        budget=BudgetConfig(
            max_consecutive_failures=3,
            max_repeated_actions=10,
            max_no_progress_steps=10,
        ),
    )

    assert result.task.status is TaskStatus.FAILED
    assert result.task.termination_reason is TerminationReason.CONSECUTIVE_FAILURES
    assert len(result.steps) == 3


def test_distinct_read_only_actions_without_progress_are_stopped(tmp_path) -> None:
    (tmp_path / "example.txt").write_text("needle\n")
    actions = [
        ToolCallAction(tool_name="list_files", arguments={"path": "."}),
        ToolCallAction(tool_name="read_file", arguments={"path": "example.txt"}),
        ToolCallAction(tool_name="search_text", arguments={"query": "needle", "path": "."}),
    ]
    registry = ToolRegistry([ListFilesTool(), ReadFileTool(), SearchTextTool()])

    result = run_loop(
        FakeLLMClient(actions),
        tmp_path,
        budget=BudgetConfig(max_no_progress_steps=3, max_repeated_actions=10),
        registry=registry,
    )

    assert result.task.status is TaskStatus.FAILED
    assert result.task.termination_reason is TerminationReason.NO_PROGRESS


def test_tool_call_budget_stops_before_extra_execution(tmp_path) -> None:
    actions = [
        ToolCallAction(tool_name="list_files", arguments={"path": "."}),
        ToolCallAction(tool_name="read_file", arguments={"path": "missing.txt"}),
    ]
    registry = ToolRegistry([ListFilesTool(), ReadFileTool()])

    result = run_loop(
        FakeLLMClient(actions),
        tmp_path,
        budget=BudgetConfig(max_tool_calls=1, max_no_progress_steps=10),
        registry=registry,
    )

    assert result.task.status is TaskStatus.BUDGET_EXCEEDED
    assert result.task.termination_reason is TerminationReason.MAX_TOOL_CALLS
    assert len(result.steps) == 1


def test_step_budget_has_explicit_termination_reason(tmp_path) -> None:
    actions = [
        ToolCallAction(tool_name="list_files", arguments={"path": "."}),
        ToolCallAction(tool_name="list_files", arguments={"path": "./"}),
    ]

    result = run_loop(
        FakeLLMClient(actions),
        tmp_path,
        budget=BudgetConfig(max_steps=2, max_repeated_actions=10, max_no_progress_steps=10),
        registry=ToolRegistry([ListFilesTool()]),
    )

    assert result.task.status is TaskStatus.BUDGET_EXCEEDED
    assert result.task.termination_reason is TerminationReason.MAX_STEPS
    assert len(result.steps) == 2


class AdvancingClient(LLMClient):
    def __init__(self, clock) -> None:
        self.clock = clock

    async def generate(self, *, messages, tools=None) -> LLMResponse:
        del messages, tools
        self.clock.value += 2.0
        return LLMResponse(action=FinishAction(summary="too late"))


class FakeClock:
    value = 0.0

    def __call__(self) -> float:
        return self.value


def test_wall_time_checked_after_model_call(tmp_path) -> None:
    clock = FakeClock()

    result = run_loop(
        AdvancingClient(clock),
        tmp_path,
        budget=BudgetConfig(max_wall_time=1.0),
        clock=clock,
    )

    assert result.task.status is TaskStatus.BUDGET_EXCEEDED
    assert result.task.termination_reason is TerminationReason.MAX_WALL_TIME
    assert result.steps == []
    assert result.elapsed_seconds == 2.0
