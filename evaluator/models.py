from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluationProfile(str, Enum):
    SINGLE_CALL = "single_call"
    BASIC_LOOP = "basic_loop"
    GUARDED_RUNTIME = "guarded_runtime"
    RELIABLE_RUNTIME = "reliable_runtime"


TaskCategory = Literal[
    "code_understanding",
    "config_repair",
    "single_file_bug",
    "multi_file_bug",
    "failure_recovery",
    "security_boundary",
]


class EvaluationTask(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    category: TaskCategory
    workspace: str
    goal: str
    mode: Literal["readonly", "write"]
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    verifiers: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=20, gt=0)
    expected_behavior: str
    expected_answer_terms: list[str] = Field(default_factory=list)
    fake_actions: str | None = None


class EvaluationRecord(StrictModel):
    run_id: str
    task_id: str
    category: TaskCategory
    profile: EvaluationProfile
    model: str
    status: str
    termination_reason: str | None = None
    task_success: bool
    verified_success: bool
    false_completion: bool
    step_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0)
    policy_violations_blocked: int = Field(default=0, ge=0)
    final_answer: str | None = None
    error: str | None = None


class EvaluationSummary(StrictModel):
    profile: EvaluationProfile
    task_count: int
    task_success_rate: float
    verified_success_rate: float
    false_completion_rate: float
    recovery_success_rate: float
    policy_block_rate: float
    average_steps: float
    average_tool_calls: float
    p50_latency_seconds: float
    p95_latency_seconds: float
    total_input_tokens: int
    total_output_tokens: int
    total_estimated_cost_usd: float
