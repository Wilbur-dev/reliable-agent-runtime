from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from llm.base import LLMClient
from llm.fake import FakeLLMClient
from llm.openai_compatible import OpenAICompatibleClient
from runtime.actions import AgentAction
from runtime.loop import InMemoryAgentLoop
from runtime.models import BudgetConfig, Task
from tools.base import ToolContext
from tools.development import ApplyPatchTool, GitDiffTool, RunLinterTool, RunTestsTool
from tools.readonly import ListFilesTool, ReadFileTool, SearchTextTool
from tools.registry import ToolRegistry
from verifiers.development import CommandExitCodeVerifier, ProtectedPathsUnchangedVerifier


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the reliable agent loop.")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--goal", required=True)
    parser.add_argument(
        "--fake-actions",
        type=Path,
        help="JSON file containing the deterministic action sequence for FakeLLMClient.",
    )
    parser.add_argument("--mode", choices=("readonly", "write"), default="readonly")
    parser.add_argument("--base-url", help="OpenAI-compatible API base URL, including /v1.")
    parser.add_argument("--model", help="Model name exposed by the compatible endpoint.")
    parser.add_argument(
        "--api-key-env",
        default="OPENAI_API_KEY",
        help="Environment variable containing the API key; the key is never accepted as an argument.",
    )
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--max-tool-calls", type=int, default=40)
    parser.add_argument("--max-wall-time", type=float, default=300.0)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--max-llm-retries", type=int, default=2)
    parser.add_argument("--input-cost-per-million", type=float, default=0.0)
    parser.add_argument("--output-cost-per-million", type=float, default=0.0)
    return parser


def load_actions(path: Path) -> list[AgentAction]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    return TypeAdapter(list[AgentAction]).validate_python(payload)


async def run_cli(args: argparse.Namespace) -> int:
    client: LLMClient
    if args.fake_actions:
        client = FakeLLMClient(load_actions(args.fake_actions))
    else:
        if not args.base_url or not args.model:
            raise SystemExit("--base-url and --model are required without --fake-actions")
        client = OpenAICompatibleClient(
            base_url=args.base_url,
            api_key=os.environ.get(args.api_key_env, ""),
            model=args.model,
            input_cost_per_million=args.input_cost_per_million,
            output_cost_per_million=args.output_cost_per_million,
        )

    tools = [ListFilesTool(), ReadFileTool(), SearchTextTool()]
    verifiers = []
    context = ToolContext(args.workspace, protected_paths=("tests",))
    if args.mode == "write":
        tools.extend(
            [
                ApplyPatchTool(),
                GitDiffTool(),
                RunTestsTool({"pytest": (sys.executable, "-B", "-m", "pytest", "-q")}),
                RunLinterTool({"ruff": (sys.executable, "-m", "ruff", "check", ".")}),
            ]
        )
        verifiers = [
            CommandExitCodeVerifier("run_tests"),
            CommandExitCodeVerifier("run_linter"),
            ProtectedPathsUnchangedVerifier(),
        ]

    loop = InMemoryAgentLoop(
        client,
        ToolRegistry(tools),
        verifiers=verifiers,
        tool_context=context,
    )
    result = await loop.run(
        Task(
            goal=args.goal,
            workspace=str(args.workspace),
            budget=BudgetConfig(
                max_steps=args.max_steps,
                max_tool_calls=args.max_tool_calls,
                max_wall_time=args.max_wall_time,
                max_tokens=args.max_tokens,
                max_cost_usd=args.max_cost_usd,
                max_llm_retries=args.max_llm_retries,
            ),
            constraints=(
                ["Do not modify tests/. Use only configured commands."]
                if args.mode == "write"
                else []
            ),
            acceptance_criteria=(
                ["Configured tests and linter must pass."] if args.mode == "write" else []
            ),
        )
    )
    print(result.model_dump_json(indent=2))
    return 0 if result.task.status.value == "COMPLETED" else 1


def main() -> int:
    return asyncio.run(run_cli(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
