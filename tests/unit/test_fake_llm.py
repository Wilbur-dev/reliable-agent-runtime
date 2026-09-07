import asyncio

import pytest

from llm.fake import FakeLLMClient, FakeLLMResponseExhaustedError
from runtime.actions import FinishAction, ToolCallAction


def test_fake_llm_returns_actions_in_order() -> None:
    client = FakeLLMClient(
        [
            ToolCallAction(tool_name="list_files"),
            FinishAction(summary="Done"),
        ]
    )

    first = asyncio.run(client.generate(messages=[]))
    second = asyncio.run(client.generate(messages=[]))

    assert first.action.type == "tool_call"
    assert second.action.type == "finish"
    assert client.call_count == 2
    assert client.remaining == 0


def test_fake_llm_fails_deterministically_when_exhausted() -> None:
    client = FakeLLMClient([])

    with pytest.raises(FakeLLMResponseExhaustedError):
        asyncio.run(client.generate(messages=[]))
