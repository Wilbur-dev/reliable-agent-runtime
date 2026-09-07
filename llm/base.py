from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from runtime.actions import LLMResponse


class LLMClient(ABC):
    @abstractmethod
    async def generate(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Return the model's next structured action."""
