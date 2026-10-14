import json

import httpx
import pytest

from harness.provider import OpenAICompatibleProvider, ProviderError, ProviderRequest


@pytest.mark.asyncio
async def test_streamed_tool_call_allows_null_delta_fields(monkeypatch):
    chunks = [
        {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "lookup", "arguments": "{\"query\":"},
                    }]
                }
            }]
        },
        {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": None,
                        "function": {"name": None, "arguments": None},
                    }]
                }
            }]
        },
        {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": "\"Agent-X\"}"},
                    }]
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    async def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("harness.provider.httpx.AsyncClient", client_factory)
    provider = OpenAICompatibleProvider("https://provider.example/v1", "test-key")
    request = ProviderRequest(model="test-model", messages=[], tools=[])

    deltas = [delta async for delta in provider.stream(request)]

    assert len(deltas) == 1
    assert deltas[0].finish_reason == "tool_calls"
    assert deltas[0].tool_calls[0].id == "call_1"
    assert deltas[0].tool_calls[0].name == "lookup"
    assert deltas[0].tool_calls[0].arguments == {"query": "Agent-X"}


@pytest.mark.asyncio
async def test_retries_inconsistent_model_not_found_before_streaming(monkeypatch):
    attempts = 0
    success = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n' \
        'data: [DONE]\n\n'

    async def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(404, json={
                "error": {"message": "The model `deepseekv4-flash-XS` does not exist."}
            })
        return httpx.Response(200, content=success, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("harness.provider.httpx.AsyncClient", client_factory)
    provider = OpenAICompatibleProvider(
        "https://provider.example/v1", "test-key", max_attempts=3,
        retry_base_delay_seconds=0, retry_max_delay_seconds=0,
    )

    deltas = [delta async for delta in provider.stream(
        ProviderRequest(model="dp", messages=[], tools=[])
    )]

    assert attempts == 2
    assert "".join(delta.text for delta in deltas) == "ok"


@pytest.mark.asyncio
async def test_does_not_retry_an_unrelated_404(monkeypatch):
    attempts = 0

    async def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, json={"error": {"message": "route not found"}})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("harness.provider.httpx.AsyncClient", client_factory)
    provider = OpenAICompatibleProvider(
        "https://provider.example/v1", "test-key", max_attempts=3,
        retry_base_delay_seconds=0, retry_max_delay_seconds=0,
    )

    with pytest.raises(ProviderError, match="provider_http_404"):
        _ = [delta async for delta in provider.stream(
            ProviderRequest(model="dp", messages=[], tools=[])
        )]
    assert attempts == 1


@pytest.mark.asyncio
async def test_does_not_retry_after_stream_output_was_emitted(monkeypatch):
    attempts = 0

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError("upstream disconnected")

    async def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, stream=BrokenStream(),
                              headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("harness.provider.httpx.AsyncClient", client_factory)
    provider = OpenAICompatibleProvider(
        "https://provider.example/v1", "test-key", max_attempts=3,
        retry_base_delay_seconds=0, retry_max_delay_seconds=0,
    )
    seen = []

    with pytest.raises(ProviderError, match="provider_connection_error"):
        async for delta in provider.stream(ProviderRequest(model="dp", messages=[], tools=[])):
            seen.append(delta.text)

    assert attempts == 1
    assert seen == ["partial"]
