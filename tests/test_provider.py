import json

import httpx
import pytest

from harness.provider import OpenAICompatibleProvider, ProviderRequest


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
