from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolCallAction(StrictModel):
    type: Literal["tool_call"] = "tool_call"
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class FinishAction(StrictModel):
    type: Literal["finish"] = "finish"
    summary: str = Field(min_length=1)


AgentAction = Annotated[ToolCallAction | FinishAction, Field(discriminator="type")]


class LLMUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0)


class LLMResponse(StrictModel):
    action: AgentAction
    usage: LLMUsage = Field(default_factory=LLMUsage)
    latency_ms: float = Field(default=0.0, ge=0)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
