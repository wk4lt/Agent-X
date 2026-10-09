from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
import json
from typing import Any, Protocol

import httpx

from contracts.models import ToolCall, Usage


@dataclass
class ProviderRequest:
    model: str
    messages: list[dict]
    tools: list[dict]
    max_output_tokens: int | None = None


@dataclass
class ProviderDelta:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str | None = None


class ProviderAdapter(Protocol):
    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderDelta]: ...


class ScriptedMockProvider:
    """A deterministic provider for development and tests; one response per turn."""
    def __init__(self, turns: Sequence[Sequence[ProviderDelta]]) -> None:
        self.turns = list(turns)
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderDelta]:
        self.requests.append(request)
        if not self.turns:
            yield ProviderDelta(text="No scripted response.", usage=Usage(finish_reason="stop"), finish_reason="stop")
            return
        for item in self.turns.pop(0):
            yield item


class EchoProvider:
    """Safe local default. It makes the stack runnable without credentials."""
    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderDelta]:
        user_messages = [message["content"] for message in request.messages if message["role"] == "user"]
        text = f"Mock provider received: {user_messages[-1] if user_messages else ''}"
        yield ProviderDelta(text=text)
        yield ProviderDelta(usage=Usage(prompt_tokens_actual=None, completion_tokens_actual=None,
                                        finish_reason="stop"), finish_reason="stop")


class ProviderError(RuntimeError):
    """A sanitized provider failure; secrets and provider response bodies never escape it."""


class OpenAICompatibleProvider:
    """OpenAI chat-completions streaming adapter, isolated from the runtime loop."""
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 90) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderDelta]:
        payload = {"model": request.model, "messages": self._wire_messages(request.messages), "tools": request.tools,
                   "stream": True, "stream_options": {"include_usage": True}}
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        partial_calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage = Usage()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                async with client.stream("POST", f"{self.base_url}/chat/completions", json=payload, headers=headers) as response:
                    if response.is_error:
                        raise ProviderError(f"provider_http_{response.status_code}")
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            raise ProviderError("provider_invalid_stream") from exc
                        raw_usage = chunk.get("usage")
                        if raw_usage:
                            usage.prompt_tokens_actual = raw_usage.get("prompt_tokens")
                            usage.completion_tokens_actual = raw_usage.get("completion_tokens")
                            details = raw_usage.get("prompt_tokens_details") or {}
                            usage.cached_tokens_actual = details.get("cached_tokens")
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta") or {}
                            content = delta.get("content") or ""
                            if content:
                                yield ProviderDelta(text=content)
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                            for item in delta.get("tool_calls") or []:
                                index = item.get("index", 0)
                                current = partial_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                                current["id"] += item.get("id", "")
                                function = item.get("function") or {}
                                current["name"] += function.get("name", "")
                                current["arguments"] += function.get("arguments", "")
        except httpx.HTTPError as exc:
            raise ProviderError("provider_connection_error") from exc
        calls: list[ToolCall] = []
        for index in sorted(partial_calls):
            call = partial_calls[index]
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError as exc:
                raise ProviderError("provider_incomplete_tool_arguments") from exc
            calls.append(ToolCall(id=call["id"] or f"provider_call_{index}", name=call["name"], arguments=arguments))
        usage.finish_reason = finish_reason
        yield ProviderDelta(tool_calls=calls, usage=usage, finish_reason=finish_reason)

    @staticmethod
    def _wire_messages(messages: list[dict]) -> list[dict]:
        """Translate internal ToolCall objects only at the provider boundary."""
        result: list[dict] = []
        for message in messages:
            wire = dict(message)
            calls = wire.get("tool_calls")
            if calls:
                wire["tool_calls"] = [
                    {"id": call["id"], "type": "function", "function": {
                        "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                    }} for call in calls
                ]
            result.append(wire)
        return result


def provider_from_settings(settings) -> ProviderAdapter:
    """Select a credentialed endpoint only after typed configuration has validated it."""
    if settings.provider == "openai_compatible":
        assert settings.llm_api_key
        return OpenAICompatibleProvider(settings.llm_base_url, settings.llm_api_key, settings.llm_timeout_seconds)
    return EchoProvider()
