import pytest
from pydantic import TypeAdapter, ValidationError

from runtime.actions import AgentAction, FinishAction, LLMResponse, ToolCallAction
from runtime.models import BudgetConfig, Task, TaskStatus

action_adapter = TypeAdapter(AgentAction)


def test_tool_call_action_parses() -> None:
    action = action_adapter.validate_python(
        {"type": "tool_call", "tool_name": "read_file", "arguments": {"path": "README.md"}}
    )

    assert action == ToolCallAction(tool_name="read_file", arguments={"path": "README.md"})


def test_finish_action_parses_in_llm_response() -> None:
    response = LLMResponse.model_validate(
        {"action": {"type": "finish", "summary": "Analysis complete"}}
    )

    assert response.action == FinishAction(summary="Analysis complete")


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "unknown", "summary": "no"},
        {"type": "tool_call", "tool_name": "read_file", "arguments": {}, "extra": True},
        {"type": "finish"},
    ],
)
def test_invalid_actions_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        action_adapter.validate_python(payload)


def test_budget_requires_positive_limits() -> None:
    with pytest.raises(ValidationError):
        BudgetConfig(max_steps=0)


def test_task_defaults_to_pending_with_a_budget() -> None:
    task = Task(goal="Inspect the repository", workspace="/workspace")

    assert task.status is TaskStatus.PENDING
    assert task.budget.max_steps > 0


def test_unknown_task_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Task.model_validate(
            {"goal": "Inspect the repository", "workspace": "/workspace", "unknown": True}
        )
