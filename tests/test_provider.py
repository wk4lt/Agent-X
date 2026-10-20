import asyncio
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
    assert deltas[0].tool_calls[0].arguments_json == '{"query":"Agent-X"}'

    replay = OpenAICompatibleProvider._wire_messages([{
        "role": "assistant",
        "content": "",
        "tool_calls": [deltas[0].tool_calls[0].model_dump()],
    }])
    assert replay[0]["tool_calls"][0]["function"]["arguments"] == '{"query":"Agent-X"}'


@pytest.mark.asyncio
async def test_reads_deepseek_cache_usage_fields(monkeypatch):
    chunk = {
        "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 2,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 20,
        },
    }
    body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"

    async def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("harness.provider.httpx.AsyncClient", client_factory)
    provider = OpenAICompatibleProvider("https://provider.example/v1", "test-key")
    deltas = [delta async for delta in provider.stream(
        ProviderRequest(model="test-model", messages=[], tools=[])
    )]

    usage = deltas[-1].usage
    assert usage is not None
    assert usage.cached_tokens_actual == 80
    assert usage.cache_miss_tokens_actual == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    '{"error":{"message":"The model `deepseekv4-flash-XS` does not exist."}}',
    "",
    "<html>route not found</html>",
])
async def test_retries_404_before_streaming(monkeypatch, body):
    attempts = 0
    success = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n' \
        'data: [DONE]\n\n'

    async def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(404, content=body)
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
@pytest.mark.parametrize("status, expected_attempts", [(404, 3), (400, 1), (401, 1), (403, 1)])
async def test_http_failures_respect_attempt_limit(monkeypatch, status, expected_attempts):
    attempts = 0

    async def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, json={"error": {"message": "route not found"}})

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

    with pytest.raises(ProviderError, match=f"provider_http_{status}"):
        _ = [delta async for delta in provider.stream(
            ProviderRequest(model="dp", messages=[], tools=[])
        )]
    assert attempts == expected_attempts


@pytest.mark.asyncio
async def test_cancelling_404_backoff_does_not_send_another_request(monkeypatch):
    attempts = 0
    sleeping = asyncio.Event()

    async def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(404)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr("harness.provider.httpx.AsyncClient", lambda **kwargs:
                        real_async_client(**kwargs, transport=httpx.MockTransport(handler)))
    real_sleep = asyncio.sleep

    async def backoff(delay):
        sleeping.set()
        await real_sleep(60)

    monkeypatch.setattr("harness.provider.asyncio.sleep", backoff)
    provider = OpenAICompatibleProvider("https://provider.example/v1", "test-key")

    async def consume():
        return [delta async for delta in provider.stream(
            ProviderRequest(model="dp", messages=[], tools=[]))]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(sleeping.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
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
