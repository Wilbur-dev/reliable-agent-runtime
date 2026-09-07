import asyncio
from pathlib import Path

from app.cli import load_actions
from llm.fake import FakeLLMClient
from runtime.loop import InMemoryAgentLoop
from runtime.models import Task, TaskStatus
from tools.readonly import ListFilesTool, ReadFileTool, SearchTextTool
from tools.registry import ToolRegistry

PROJECT_ROOT = Path(__file__).parents[2]


def test_phase_1_acceptance_trajectory() -> None:
    workspace = PROJECT_ROOT / "examples" / "sample_repo"
    actions = load_actions(PROJECT_ROOT / "examples" / "phase1_actions.json")
    loop = InMemoryAgentLoop(
        FakeLLMClient(actions),
        ToolRegistry([ListFilesTool(), ReadFileTool(), SearchTextTool()]),
    )

    result = asyncio.run(
        loop.run(
            Task(
                goal="Find the order discount logic and explain the rules.",
                workspace=str(workspace),
            )
        )
    )

    assert result.task.status is TaskStatus.COMPLETED
    assert [step.action.type for step in result.steps] == [
        "tool_call",
        "tool_call",
        "tool_call",
        "finish",
    ]
    assert [step.action.tool_name for step in result.steps[:-1]] == [
        "list_files",
        "search_text",
        "read_file",
    ]
    assert all(step.tool_result and step.tool_result.success for step in result.steps[:-1])
    assert "gold customers 10%" in (result.summary or "")
