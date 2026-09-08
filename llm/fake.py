from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Any

from llm.base import LLMClient
from llm.errors import LLMError
from runtime.actions import FinishAction, LLMResponse, ToolCallAction


class FakeLLMResponseExhaustedError(LLMError):
    """Raised when a fake client has no configured responses remaining."""

    error_type = "fake_llm_response_exhausted"


class FakeLLMClient(LLMClient):
    def __init__(
        self,
        responses: Iterable[LLMResponse | ToolCallAction | FinishAction],
    ) -> None:
        self._responses = deque(
            response
            if isinstance(response, LLMResponse)
            else LLMResponse(action=response, raw_metadata={"source": "fake"})
            for response in responses
        )
        self.call_count = 0

    @property
    def remaining(self) -> int:
        return len(self._responses)

    async def generate(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        del messages, tools
        self.call_count += 1
        if not self._responses:
            raise FakeLLMResponseExhaustedError("Fake LLM response sequence is exhausted")
        return self._responses.popleft()
