from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
import json
import logging
import random
from typing import Any, Protocol

import httpx

from contracts.models import ToolCall, Usage


LOG = logging.getLogger("agent_x.provider")
RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}


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


class _RetryRequest(Exception):
    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class OpenAICompatibleProvider:
    """OpenAI chat-completions streaming adapter, isolated from the runtime loop."""
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 90, *, max_attempts: int = 3,
                 retry_base_delay_seconds: float = 0.5, retry_max_delay_seconds: float = 4) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderDelta]:
        payload = {"model": request.model, "messages": self._wire_messages(request.messages), "tools": request.tools,
                   "stream": True, "stream_options": {"include_usage": True}}
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            for attempt in range(1, self.max_attempts + 1):
                partial_calls: dict[int, dict[str, Any]] = {}
                finish_reason: str | None = None
                usage = Usage()
                emitted = False
                try:
                    async with client.stream(
                        "POST", f"{self.base_url}/chat/completions", json=payload, headers=headers
                    ) as response:
                        if response.is_error:
                            body = (await response.aread())[:4_096].decode("utf-8", errors="replace")
                            if (attempt < self.max_attempts
                                    and self._retryable_response(response.status_code, body)):
                                raise _RetryRequest(
                                    f"http_{response.status_code}", self._retry_after(response)
                                )
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
                                    emitted = True
                                    yield ProviderDelta(text=content)
                                if choice.get("finish_reason"):
                                    finish_reason = choice["finish_reason"]
                                for item in delta.get("tool_calls") or []:
                                    index = item.get("index", 0)
                                    current = partial_calls.setdefault(
                                        index, {"id": "", "name": "", "arguments": ""}
                                    )
                                    current["id"] += item.get("id") or ""
                                    function = item.get("function") or {}
                                    current["name"] += function.get("name") or ""
                                    current["arguments"] += function.get("arguments") or ""
                except _RetryRequest as retry:
                    await self._wait_before_retry(request.model, attempt, retry.reason, retry.retry_after)
                    continue
                except httpx.HTTPError as exc:
                    if attempt < self.max_attempts and not emitted:
                        await self._wait_before_retry(request.model, attempt, type(exc).__name__)
                        continue
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
                return
        raise ProviderError("provider_retry_exhausted")

    @staticmethod
    def _retryable_response(status_code: int, body: str) -> bool:
        if status_code in RETRYABLE_HTTP_STATUSES:
            return True
        normalized = body.lower()
        return status_code == 404 and "model" in normalized and "does not exist" in normalized

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    async def _wait_before_retry(self, model: str, attempt: int, reason: str,
                                 retry_after: float | None = None) -> None:
        exponential = self.retry_base_delay_seconds * (2 ** (attempt - 1))
        delay = max(exponential, retry_after or 0.0)
        if delay:
            delay += random.uniform(0, min(delay * 0.25, 1.0))
        delay = min(delay, self.retry_max_delay_seconds)
        LOG.warning("provider request retrying model=%s attempt=%d/%d reason=%s delay_seconds=%.2f",
                    model, attempt + 1, self.max_attempts, reason, delay)
        if delay:
            await asyncio.sleep(delay)

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
        return OpenAICompatibleProvider(
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_timeout_seconds,
            max_attempts=settings.llm_max_attempts,
            retry_base_delay_seconds=settings.llm_retry_base_delay_seconds,
            retry_max_delay_seconds=settings.llm_retry_max_delay_seconds,
        )
    return EchoProvider()
