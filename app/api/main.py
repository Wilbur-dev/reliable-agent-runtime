from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from llm.fake import FakeLLMClient
from persistence.database import SQLiteStore
from policies.engine import PolicyEngine
from runtime.actions import AgentAction
from runtime.context import ContextBuilder, ContextConfig
from runtime.loop import AgentStepRecord, InMemoryAgentLoop
from runtime.models import BudgetConfig, Task, TaskStatus, TerminationReason, ToolResult
from runtime.observability import RuntimeMetrics
from sandbox.docker import DockerCommandRunner, DockerSandboxConfig
from tools.base import ToolContext
from tools.development import ApplyPatchTool, GitDiffTool, RunLinterTool, RunTestsTool
from tools.readonly import ListFilesTool, ReadFileTool, SearchTextTool
from tools.registry import ToolRegistry
from verifiers.development import CommandExitCodeVerifier, ProtectedPathsUnchangedVerifier


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTaskRequest(StrictModel):
    goal: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    mode: str = Field(default="readonly", pattern="^(readonly|write)$")
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    fake_actions: list[AgentAction] = Field(default_factory=list)
    sandbox: bool = True
    sandbox_image: str = "reliable-agent-runtime-sandbox:phase5"
    context: ContextConfig = Field(default_factory=ContextConfig)


class CancelResponse(StrictModel):
    id: str
    status: TaskStatus


class ApprovalRequest(StrictModel):
    step_id: int = Field(gt=0)


class RejectionRequest(ApprovalRequest):
    reason: str = Field(min_length=1, max_length=2000)


class RuntimeService:
    def __init__(self, store: SQLiteStore, metrics: RuntimeMetrics | None = None) -> None:
        self.store = store
        self.metrics = metrics or RuntimeMetrics()
        self.locks: dict[str, asyncio.Lock] = {}
        self.recovered_task_ids = store.recover_interrupted()

    def create(self, request: CreateTaskRequest) -> Task:
        task = Task(
            goal=request.goal,
            workspace=request.workspace,
            budget=request.budget,
            constraints=request.constraints,
            acceptance_criteria=request.acceptance_criteria,
        )
        self.store.create_task(
            task,
            runtime_config={
                "mode": request.mode,
                "fake_actions": [action.model_dump(mode="json") for action in request.fake_actions],
                "sandbox": request.sandbox,
                "sandbox_image": request.sandbox_image,
                "context": request.context.model_dump(mode="json"),
            },
        )
        self.metrics.observe_task_created()
        return task

    async def run(self, task_id: str) -> Task:
        lock = self.locks.setdefault(task_id, asyncio.Lock())
        if lock.locked():
            raise HTTPException(status.HTTP_409_CONFLICT, "Task is already running")
        async with lock:
            task = self.store.get_task(task_id)
            if task is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
            if task.status is TaskStatus.CANCELLED:
                raise HTTPException(status.HTTP_409_CONFLICT, "Task was cancelled")
            if task.status is TaskStatus.WAITING_APPROVAL:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Task is waiting for an approval decision",
                )
            config = self.store.get_runtime_config(task_id)
            actions = config.get("fake_actions", [])
            steps = self.store.list_steps(task_id)
            consumed = sum(row["status"] in {"SUCCEEDED", "FAILED"} for row in steps)
            remaining = actions[consumed:]
            if not remaining:
                raise HTTPException(status.HTTP_409_CONFLICT, "No runnable actions remain")

            tools = [ListFilesTool(), ReadFileTool(), SearchTextTool()]
            verifiers = []
            if config.get("mode") == "write":
                if config.get("sandbox", True):
                    test_commands = {
                        "unittest": (
                            "python",
                            "-B",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "tests",
                        )
                    }
                    lint_commands = {"compileall": ("python", "-B", "-m", "compileall", "-q", ".")}
                else:
                    test_commands = {"pytest": ("python", "-B", "-m", "pytest", "-q")}
                    lint_commands = {"ruff": ("python", "-m", "ruff", "check", ".")}
                tools.extend(
                    [
                        ApplyPatchTool(),
                        GitDiffTool(),
                        RunTestsTool(test_commands),
                        RunLinterTool(lint_commands),
                    ]
                )
                verifiers = [
                    CommandExitCodeVerifier("run_tests"),
                    CommandExitCodeVerifier("run_linter"),
                    ProtectedPathsUnchangedVerifier(),
                ]
            messages = self.store.load_messages(task_id) or None
            record = self.store.get_task_record(task_id)
            action_adapter = TypeAdapter(AgentAction)
            history_steps = [
                AgentStepRecord(
                    sequence=row["sequence"],
                    action=action_adapter.validate_python(row["action"]),
                    tool_result=(
                        ToolResult.model_validate(row["result"]) if row["result"] else None
                    ),
                )
                for row in steps
                if row["status"] in {"SUCCEEDED", "FAILED"}
            ]
            command_runner = None
            if config.get("mode") == "write" and config.get("sandbox", True):
                command_runner = DockerCommandRunner(
                    DockerSandboxConfig(image=config.get("sandbox_image"))
                )
            loop = InMemoryAgentLoop(
                FakeLLMClient(remaining),
                ToolRegistry(tools),
                verifiers=verifiers,
                tool_context=ToolContext(
                    Path(task.workspace),
                    protected_paths=("tests",),
                    command_runner=command_runner,
                ),
                checkpoint_store=self.store,
                resume_messages=messages,
                sequence_offset=max((row["sequence"] for row in steps), default=0),
                initial_metrics=record["metrics"] if record else {},
                initial_tool_call_count=sum(
                    row["action"].get("type") == "tool_call"
                    and row["status"] in {"SUCCEEDED", "FAILED"}
                    for row in steps
                ),
                history_steps=history_steps,
                policy_engine=PolicyEngine(),
                context_builder=ContextBuilder(
                    ContextConfig.model_validate(config.get("context", {}))
                ),
            )
            result = await loop.run(task)
            self.metrics.observe_run(result, initial_metrics=record["metrics"] if record else {})
            return result.task

    def cancel(self, task_id: str) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
        self.store.set_task_status(
            task_id,
            TaskStatus.CANCELLED,
            termination_reason=TerminationReason.CANCELLED.value,
        )
        return self.store.get_task(task_id)

    def decide_approval(
        self, task_id: str, step_id: int, *, approved: bool, reason: str | None = None
    ) -> dict[str, object]:
        if self.store.get_task(task_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
        try:
            return self.store.decide_approval(
                task_id, step_id, approved=approved, rejection_reason=reason
            )
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval step not found") from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


def create_app(database_url: str | None = None) -> FastAPI:
    app = FastAPI(title="Reliable Agent Runtime", version="0.6.0")
    service = RuntimeService(
        SQLiteStore(database_url or os.environ.get("DATABASE_URL", "sqlite:///reliable_agent.db"))
    )
    app.state.runtime_service = service

    def get_service() -> RuntimeService:
        return app.state.runtime_service

    service_dependency = Depends(get_service)

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "recovered_tasks": len(service.recovered_task_ids)}

    @app.get("/metrics")
    def metrics(runtime: RuntimeService = service_dependency) -> Response:
        return Response(
            content=runtime.metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.post("/tasks", status_code=status.HTTP_201_CREATED)
    def create_task(
        request: CreateTaskRequest,
        runtime: RuntimeService = service_dependency,
    ) -> dict[str, object]:
        return runtime.create(request).model_dump(mode="json")

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str, runtime: RuntimeService = service_dependency) -> dict[str, object]:
        record = runtime.store.get_task_record(task_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
        return record

    @app.get("/tasks/{task_id}/steps")
    def get_steps(
        task_id: str, runtime: RuntimeService = service_dependency
    ) -> list[dict[str, object]]:
        if runtime.store.get_task(task_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
        return runtime.store.list_steps(task_id)

    @app.get("/tasks/{task_id}/approvals")
    def get_approvals(
        task_id: str, runtime: RuntimeService = service_dependency
    ) -> list[dict[str, object]]:
        if runtime.store.get_task(task_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
        return runtime.store.list_approvals(task_id)

    @app.get("/tasks/{task_id}/context-compactions")
    def get_context_compactions(
        task_id: str, runtime: RuntimeService = service_dependency
    ) -> list[dict[str, object]]:
        if runtime.store.get_task(task_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
        return runtime.store.list_context_compactions(task_id)

    @app.get("/tasks/{task_id}/events")
    def get_events(
        task_id: str, runtime: RuntimeService = service_dependency
    ) -> list[dict[str, object]]:
        if runtime.store.get_task(task_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
        return runtime.store.list_events(task_id)

    @app.get("/tasks/{task_id}/results/{result_id}")
    def get_full_result(
        task_id: str,
        result_id: str,
        runtime: RuntimeService = service_dependency,
    ) -> dict[str, str]:
        if runtime.store.get_task(task_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
        output = runtime.store.get_tool_output(task_id, result_id)
        if output is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Tool result not found")
        return {"result_id": result_id, "output": output}

    @app.post("/tasks/{task_id}/run")
    async def run_task(
        task_id: str, runtime: RuntimeService = service_dependency
    ) -> dict[str, object]:
        return (await runtime.run(task_id)).model_dump(mode="json")

    @app.post("/tasks/{task_id}/cancel", response_model=CancelResponse)
    def cancel_task(task_id: str, runtime: RuntimeService = service_dependency) -> CancelResponse:
        task = runtime.cancel(task_id)
        return CancelResponse(id=str(task.id), status=task.status)

    @app.post("/tasks/{task_id}/approve")
    def approve_task(
        task_id: str,
        request: ApprovalRequest,
        runtime: RuntimeService = service_dependency,
    ) -> dict[str, object]:
        return runtime.decide_approval(task_id, request.step_id, approved=True)

    @app.post("/tasks/{task_id}/reject")
    def reject_task(
        task_id: str,
        request: RejectionRequest,
        runtime: RuntimeService = service_dependency,
    ) -> dict[str, object]:
        return runtime.decide_approval(
            task_id, request.step_id, approved=False, reason=request.reason
        )

    return app


app = create_app()
