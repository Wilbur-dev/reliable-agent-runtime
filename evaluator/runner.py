from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from evaluator.models import EvaluationProfile, EvaluationRecord, EvaluationTask
from llm.base import LLMClient
from persistence.database import SQLiteStore
from policies.engine import PolicyEngine
from runtime.actions import FinishAction, ToolCallAction
from runtime.context import ContextBuilder
from runtime.loop import InMemoryAgentLoop
from runtime.models import BudgetConfig, Task, TaskStatus
from tools.base import ToolContext
from tools.development import ApplyPatchTool, GitDiffTool, RunLinterTool, RunTestsTool
from tools.readonly import ListFilesTool, ReadFileTool, SearchTextTool
from tools.registry import ToolRegistry
from verifiers.development import CommandExitCodeVerifier, ProtectedPathsUnchangedVerifier


def profile_capabilities(profile: EvaluationProfile) -> dict[str, bool]:
    return {
        "tools": profile is not EvaluationProfile.SINGLE_CALL,
        "budgets": profile
        in {EvaluationProfile.GUARDED_RUNTIME, EvaluationProfile.RELIABLE_RUNTIME},
        "policy": profile
        in {EvaluationProfile.GUARDED_RUNTIME, EvaluationProfile.RELIABLE_RUNTIME},
        "persistence": profile is EvaluationProfile.RELIABLE_RUNTIME,
        "context": profile is EvaluationProfile.RELIABLE_RUNTIME,
        "verification_feedback": profile is EvaluationProfile.RELIABLE_RUNTIME,
    }


class EvaluationRunner:
    def __init__(self, *, project_root: str | Path, output_dir: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.output_dir = Path(output_dir).resolve()

    async def run_task(
        self,
        task_spec: EvaluationTask,
        profile: EvaluationProfile,
        client: LLMClient,
        *,
        model: str,
        run_id: str | None = None,
    ) -> EvaluationRecord:
        run_id = run_id or uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix=f"rar-eval-{task_spec.id}-") as temp:
            workspace = Path(temp) / "workspace"
            shutil.copytree(self.project_root / task_spec.workspace, workspace)
            self._initialize_git(workspace)
            runtime_task = Task(
                goal=task_spec.goal,
                workspace=str(workspace),
                constraints=task_spec.constraints,
                acceptance_criteria=task_spec.acceptance_criteria,
                budget=BudgetConfig(
                    max_steps=(
                        1
                        if profile is EvaluationProfile.SINGLE_CALL
                        else (
                            task_spec.max_steps if profile_capabilities(profile)["budgets"] else 50
                        )
                    ),
                    max_tool_calls=80,
                    max_wall_time=600,
                    max_repeated_actions=5,
                    max_no_progress_steps=12,
                ),
            )
            tools = self._tools(task_spec.mode)
            context = ToolContext(workspace, protected_paths=("tests",))
            feedback_verifiers = (
                self._verifiers(task_spec.mode)
                if profile_capabilities(profile)["verification_feedback"]
                else []
            )
            store = None
            if profile_capabilities(profile)["persistence"]:
                store = SQLiteStore(f"sqlite:///{Path(temp) / 'evaluation.db'}")
                store.create_task(runtime_task)
            loop = InMemoryAgentLoop(
                client,
                ToolRegistry(tools if profile_capabilities(profile)["tools"] else []),
                verifiers=feedback_verifiers,
                tool_context=context,
                checkpoint_store=store,
                policy_engine=(PolicyEngine() if profile_capabilities(profile)["policy"] else None),
                context_builder=(
                    ContextBuilder() if profile_capabilities(profile)["context"] else None
                ),
            )
            result = await loop.run(runtime_task)
            finish_actions = [
                step.action for step in result.steps if isinstance(step.action, FinishAction)
            ]
            model_finished = bool(finish_actions)
            policy_blocks = sum(
                bool(
                    step.tool_result
                    and step.tool_result.error_type in {"policy_denied", "policy_rejected"}
                )
                for step in result.steps
            ) + int(result.task.status is TaskStatus.WAITING_APPROVAL)
            verified_success = self._assess(task_spec, result, policy_blocks=policy_blocks)
            record = EvaluationRecord(
                run_id=run_id,
                task_id=task_spec.id,
                category=task_spec.category,
                profile=profile,
                model=model,
                status=result.task.status.value,
                termination_reason=(
                    result.task.termination_reason.value if result.task.termination_reason else None
                ),
                task_success=result.task.status is TaskStatus.COMPLETED,
                verified_success=verified_success,
                false_completion=model_finished and not verified_success,
                step_count=len(result.steps),
                tool_call_count=sum(
                    isinstance(step.action, ToolCallAction) for step in result.steps
                ),
                retry_count=result.llm_retry_count,
                elapsed_seconds=result.elapsed_seconds,
                input_tokens=result.total_input_tokens,
                output_tokens=result.total_output_tokens,
                estimated_cost_usd=result.total_estimated_cost_usd,
                policy_violations_blocked=policy_blocks,
                final_answer=finish_actions[-1].summary if finish_actions else None,
                error=result.error,
            )
            self._write_record(record)
            return record

    @staticmethod
    def _tools(mode: str):
        tools = [ListFilesTool(), ReadFileTool(), SearchTextTool()]
        if mode == "write":
            tools.extend(
                [
                    ApplyPatchTool(),
                    GitDiffTool(),
                    RunTestsTool({"pytest": (sys.executable, "-B", "-m", "pytest", "-q")}),
                    RunLinterTool({"ruff": (sys.executable, "-m", "ruff", "check", ".")}),
                ]
            )
        return tools

    @staticmethod
    def _verifiers(mode: str):
        if mode == "readonly":
            return []
        return [
            CommandExitCodeVerifier("run_tests"),
            CommandExitCodeVerifier("run_linter"),
            ProtectedPathsUnchangedVerifier(),
        ]

    @staticmethod
    def _assess(task_spec, result, *, policy_blocks: int = 0) -> bool:
        if task_spec.category == "code_understanding":
            summaries = [
                step.action.summary.casefold()
                for step in result.steps
                if isinstance(step.action, FinishAction)
            ]
            return (
                result.task.status is TaskStatus.COMPLETED
                and bool(summaries)
                and all(
                    term.casefold() in summaries[-1] for term in task_spec.expected_answer_terms
                )
            )
        if "policy_block" in task_spec.verifiers:
            return policy_blocks > 0
        if result.verifications:
            return all(verification.success for verification in result.verifications[-3:])
        successful_tools = {
            step.action.tool_name
            for step in result.steps
            if isinstance(step.action, ToolCallAction)
            and step.tool_result
            and step.tool_result.success
        }
        required = {name for name in task_spec.verifiers if name in {"run_tests", "run_linter"}}
        return required.issubset(successful_tools) and bool(required)

    @staticmethod
    def _initialize_git(workspace: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        subprocess.run(["git", "add", "."], cwd=workspace, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Evaluation",
                "-c",
                "user.email=evaluation@example.invalid",
                "commit",
                "-qm",
                "evaluation baseline",
            ],
            cwd=workspace,
            check=True,
        )

    def _write_record(self, record: EvaluationRecord) -> None:
        destination = self.output_dir / "raw" / record.run_id / record.profile.value
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / f"{record.task_id}.json"
        path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")


def run(coroutine):
    return asyncio.run(coroutine)
