from __future__ import annotations

import json
from time import monotonic
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from llm.base import LLMClient
from llm.errors import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMRateLimitError,
    LLMResponseError,
    LLMServiceError,
    LLMTimeoutError,
)
from runtime.actions import AgentAction, FinishAction, LLMResponse, LLMUsage, ToolCallAction

action_adapter = TypeAdapter(AgentAction)


class OpenAICompatibleClient(LLMClient):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60,
        temperature: float = 0.0,
        max_output_tokens: int = 256,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url or not model:
            raise LLMConfigurationError("base_url and model are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self._client = client

    async def generate(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        request_tools = [{"type": "function", "function": schema} for schema in (tools or [])]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_openai_messages(messages),
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
        }
        if request_tools:
            payload["tools"] = request_tools
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = False

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        started = monotonic()
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"Model request timed out after {self.timeout_seconds:g}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMServiceError(f"Model request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        latency_ms = (monotonic() - started) * 1000
        if response.status_code in (401, 403):
            raise LLMAuthenticationError("Model endpoint rejected the credentials")
        if response.status_code == 429:
            raise LLMRateLimitError("Model endpoint rate limit exceeded")
        if response.status_code >= 500:
            raise LLMServiceError(f"Model endpoint returned HTTP {response.status_code}")
        if response.status_code >= 400:
            raise LLMResponseError(
                f"Model endpoint returned HTTP {response.status_code}: {response.text}"
            )

        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            action = self._parse_action(message)
            usage = body.get("usage") or {}
            return LLMResponse(
                action=action,
                usage=LLMUsage(
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                ),
                latency_ms=latency_ms,
                raw_metadata={
                    "id": body.get("id"),
                    "model": body.get("model", self.model),
                    "finish_reason": choice.get("finish_reason"),
                },
            )
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            raise LLMResponseError(f"Invalid model response: {exc}") from exc

    @staticmethod
    def _parse_action(message: dict[str, Any]) -> AgentAction:
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            call = tool_calls[0]
            function = call["function"]
            arguments = function.get("arguments") or "{}"
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            return ToolCallAction(tool_name=function["name"], arguments=arguments)

        content = message.get("content") or ""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return FinishAction(summary=content)
        return action_adapter.validate_python(parsed)

    @staticmethod
    def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []
        pending_tool_call_id: str | None = None
        for index, message in enumerate(messages):
            role = message["role"]
            content = message.get("content")
            if role == "assistant" and isinstance(content, dict):
                if content.get("type") == "tool_call":
                    pending_tool_call_id = f"runtime_call_{index}"
                    rendered.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": pending_tool_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": content["tool_name"],
                                        "arguments": json.dumps(content.get("arguments", {})),
                                    },
                                }
                            ],
                        }
                    )
                    continue
                content = json.dumps(content)
            if role == "tool":
                if pending_tool_call_id is None:
                    rendered.append({"role": "user", "content": json.dumps(content)})
                else:
                    rendered.append(
                        {
                            "role": "tool",
                            "tool_call_id": pending_tool_call_id,
                            "content": json.dumps(content),
                        }
                    )
                    pending_tool_call_id = None
                continue
            if not isinstance(content, str) and content is not None:
                content = json.dumps(content)
            rendered.append({"role": role, "content": content})
        return rendered
