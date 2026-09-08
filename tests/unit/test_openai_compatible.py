import asyncio
import json

import httpx
import pytest

from llm.errors import LLMAuthenticationError, LLMConfigurationError, LLMTimeoutError
from llm.openai_compatible import OpenAICompatibleClient
from runtime.actions import FinishAction, ToolCallAction


def test_parses_openai_tool_call_and_usage() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["tools"][0]["function"]["name"] == "read_file"
        assert payload["temperature"] == 0.0
        assert payload["max_tokens"] == 256
        assert payload["parallel_tool_calls"] is False
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "model": "test-model",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"README.md"}',
                                    }
                                }
                            ]
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    client = OpenAICompatibleClient(
        base_url="http://model.test/v1",
        api_key="secret",
        model="test-model",
        input_cost_per_million=2.0,
        output_cost_per_million=4.0,
        client=http_client,
    )

    response = asyncio.run(
        client.generate(
            messages=[{"role": "user", "content": "read it"}],
            tools=[{"name": "read_file", "description": "read", "parameters": {}}],
        )
    )
    asyncio.run(http_client.aclose())

    assert response.action == ToolCallAction(tool_name="read_file", arguments={"path": "README.md"})
    assert response.usage.input_tokens == 10
    assert response.usage.estimated_cost_usd == pytest.approx(0.000036)
    assert response.raw_metadata["cost_rule"]["input_usd_per_million_tokens"] == 2.0
    assert response.raw_metadata["id"] == "response-1"


def test_plain_content_becomes_finish_action() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Work complete"}, "finish_reason": "stop"}]},
        )
    )
    http_client = httpx.AsyncClient(transport=transport)
    client = OpenAICompatibleClient(
        base_url="http://model.test/v1",
        api_key="secret",
        model="test-model",
        client=http_client,
    )

    response = asyncio.run(client.generate(messages=[], tools=[]))
    asyncio.run(http_client.aclose())

    assert response.action == FinishAction(summary="Work complete")


def test_negative_token_price_is_rejected() -> None:
    with pytest.raises(LLMConfigurationError):
        OpenAICompatibleClient(
            base_url="http://model.test/v1",
            api_key="",
            model="test-model",
            input_cost_per_million=-1,
        )


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [(401, LLMAuthenticationError)],
)
def test_maps_http_errors(status_code, error_type) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status_code))
    http_client = httpx.AsyncClient(transport=transport)
    client = OpenAICompatibleClient(
        base_url="http://model.test/v1",
        api_key="bad",
        model="test-model",
        client=http_client,
    )

    with pytest.raises(error_type):
        asyncio.run(client.generate(messages=[], tools=[]))
    asyncio.run(http_client.aclose())


def test_maps_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleClient(
        base_url="http://model.test/v1",
        api_key="secret",
        model="test-model",
        client=http_client,
    )

    with pytest.raises(LLMTimeoutError):
        asyncio.run(client.generate(messages=[], tools=[]))
    asyncio.run(http_client.aclose())


def test_runtime_messages_are_converted_to_openai_tool_protocol() -> None:
    messages = [
        {"role": "user", "content": {"goal": "read"}},
        {
            "role": "assistant",
            "content": {
                "type": "tool_call",
                "tool_name": "read_file",
                "arguments": {"path": "README.md"},
            },
        },
        {"role": "tool", "name": "read_file", "content": {"success": True}},
    ]

    rendered = OpenAICompatibleClient._to_openai_messages(messages)

    assert rendered[0]["content"] == '{"goal": "read"}'
    assert rendered[1]["tool_calls"][0]["function"]["name"] == "read_file"
    assert rendered[2]["tool_call_id"] == rendered[1]["tool_calls"][0]["id"]
