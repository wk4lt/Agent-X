from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx

from contracts.models import CreateRunRequest, RunView, SessionEntry


class HarnessClient:
    """HTTP-only integration boundary; Backend never imports Harness runtime modules."""
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("HARNESS_URL", "http://127.0.0.1:8001")).rstrip("/")
        self.token = token if token is not None else os.getenv("HARNESS_INTERNAL_TOKEN")

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Internal-Token": self.token} if self.token else {}

    @staticmethod
    def _client(*, timeout: float | None = 10) -> httpx.AsyncClient:
        # This traffic is always loopback service-to-service traffic. It must not
        # inherit ALL_PROXY/HTTPS_PROXY, even when model traffic intentionally does.
        return httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def create_run(self, request: CreateRunRequest) -> RunView:
        async with self._client() as client:
            # Do not serialize local contract defaults. The Harness owns operational
            # defaults (model and budget) via its validated environment settings.
            response = await client.post(
                f"{self.base_url}/internal/runs",
                json=request.model_dump(mode="json", exclude_unset=True),
                headers=self.headers,
            )
            response.raise_for_status()
            return RunView.model_validate(response.json())

    async def get_run(self, run_id: str) -> RunView:
        async with self._client() as client:
            response = await client.get(f"{self.base_url}/internal/runs/{run_id}", headers=self.headers)
            response.raise_for_status()
            return RunView.model_validate(response.json())

    async def cancel(self, run_id: str) -> RunView:
        async with self._client() as client:
            response = await client.post(f"{self.base_url}/internal/runs/{run_id}/cancel", headers=self.headers)
            response.raise_for_status()
            return RunView.model_validate(response.json())

    async def resume(self, run_id: str) -> RunView:
        async with self._client() as client:
            response = await client.post(f"{self.base_url}/internal/runs/{run_id}/resume", headers=self.headers)
            response.raise_for_status()
            return RunView.model_validate(response.json())

    async def session_entries(self, run_id: str) -> list[SessionEntry]:
        async with self._client() as client:
            response = await client.get(f"{self.base_url}/internal/runs/{run_id}/session", headers=self.headers)
            response.raise_for_status()
            return [SessionEntry.model_validate(item) for item in response.json()]

    async def skills(self) -> list[dict]:
        async with self._client() as client:
            response = await client.get(f"{self.base_url}/internal/skills", headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def events(self, run_id: str, after_sequence: int) -> AsyncIterator[bytes]:
        async with self._client(timeout=None) as client:
            async with client.stream("GET", f"{self.base_url}/internal/runs/{run_id}/events",
                                     params={"after_sequence": after_sequence}, headers=self.headers) as response:
                response.raise_for_status()
                async for chunk in response.aiter_raw():
                    yield chunk
