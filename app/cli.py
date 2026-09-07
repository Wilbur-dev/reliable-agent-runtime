from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from llm.fake import FakeLLMClient
from runtime.actions import AgentAction
from runtime.loop import InMemoryAgentLoop
from runtime.models import Task
from tools.readonly import ListFilesTool, ReadFileTool, SearchTextTool
from tools.registry import ToolRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the phase 1 read-only agent loop.")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--goal", required=True)
    parser.add_argument(
        "--fake-actions",
        required=True,
        type=Path,
        help="JSON file containing the deterministic action sequence for FakeLLMClient.",
    )
    return parser


def load_actions(path: Path) -> list[AgentAction]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    return TypeAdapter(list[AgentAction]).validate_python(payload)


async def run_cli(args: argparse.Namespace) -> int:
    registry = ToolRegistry([ListFilesTool(), ReadFileTool(), SearchTextTool()])
    loop = InMemoryAgentLoop(FakeLLMClient(load_actions(args.fake_actions)), registry)
    result = await loop.run(Task(goal=args.goal, workspace=str(args.workspace)))
    print(result.model_dump_json(indent=2))
    return 0 if result.task.status.value == "COMPLETED" else 1


def main() -> int:
    return asyncio.run(run_cli(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
