from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict, Field

from runtime.models import Task
from tools.base import ToolContext


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verifier: str
    success: bool
    evidence: str
    metadata: dict[str, object] = Field(default_factory=dict)


class Verifier(ABC):
    name: str

    @abstractmethod
    async def verify(
        self,
        *,
        task: Task,
        context: ToolContext,
        steps: list[object],
    ) -> VerificationResult:
        """Return deterministic evidence for a model finish request."""
