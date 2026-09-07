from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class TerminationReason(str, Enum):
    VERIFIED_COMPLETE = "VERIFIED_COMPLETE"
    CANCELLED = "CANCELLED"
    MAX_STEPS = "MAX_STEPS"
    MAX_TOOL_CALLS = "MAX_TOOL_CALLS"
    MAX_WALL_TIME = "MAX_WALL_TIME"
    MAX_TOKENS = "MAX_TOKENS"
    MAX_COST = "MAX_COST"
    CONSECUTIVE_FAILURES = "CONSECUTIVE_FAILURES"
    REPEATED_ACTIONS = "REPEATED_ACTIONS"
    NO_PROGRESS = "NO_PROGRESS"
    UNRECOVERABLE_ERROR = "UNRECOVERABLE_ERROR"


class BudgetConfig(StrictModel):
    max_steps: int = Field(default=20, gt=0)
    max_tool_calls: int = Field(default=40, gt=0)
    max_wall_time: float = Field(default=300.0, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, gt=0)


class Task(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    goal: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    status: TaskStatus = TaskStatus.PENDING
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    termination_reason: TerminationReason | None = None


class Step(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    sequence: int = Field(ge=0)
    status: StepStatus = StepStatus.PENDING
    action_type: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class ToolResult(StrictModel):
    success: bool
    output: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    exit_code: int | None = None
    truncated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
