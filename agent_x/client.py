"""Small synchronous client for submitting work to Agent-X from Python scripts."""
from __future__ import annotations

import time
from types import TracebackType
from uuid import uuid4

import httpx

from contracts.models import RunStatus, TaskView, TERMINAL_STATUSES


class AgentXError(RuntimeError):
    """Base error raised by the Agent-X client."""


class AgentXAPIError(AgentXError):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Agent-X API returned HTTP {status_code}: {detail}")


class AgentXTimeoutError(AgentXError):
    """A task did not reach a terminal state before the client deadline."""


class AgentXTaskError(AgentXError):
    def __init__(self, task: TaskView) -> None:
        self.task = task
        run = task.run
        status = run.status.value if run else "unknown"
        reason = run.stop_reason if run else None
        message = f"Agent-X task {task.task_id} ended with status {status}"
        super().__init__(f"{message}: {reason or 'no reason provided'}")


class AgentXClient:
    """Blocking convenience client built on the existing asynchronous Task API."""

    def __init__(self, base_url: str, api_key: str | None = None, *, request_timeout: float = 30.0,
                 poll_interval: float = 0.5, client: httpx.Client | None = None) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        if client is not None:
            self._client = client
            if headers:
                self._client.headers.update(headers)
        else:
            self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers,
                                        timeout=request_timeout)
        self._owns_client = client is None
        self.poll_interval = poll_interval

    def __enter__(self) -> "AgentXClient":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text or response.reason_phrase
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
        return str(body)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise AgentXError(f"Agent-X request failed: {exc}") from exc
        if response.is_error:
            raise AgentXAPIError(response.status_code, self._detail(response))
        return response

    def submit(self, input: str, *, selected_skills: list[str] | None = None,
               project_id: str = "default", conversation_id: str | None = None,
               idempotency_key: str | None = None) -> TaskView:
        payload = {
            "input": input,
            "selected_skills": selected_skills or [],
            "project_id": project_id,
        }
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        response = self._request("POST", "/api/tasks", json=payload,
                                 headers={"Idempotency-Key": idempotency_key or str(uuid4())})
        return TaskView.model_validate(response.json())

    def get_task(self, task_id: str) -> TaskView:
        return TaskView.model_validate(self._request("GET", f"/api/tasks/{task_id}").json())

    def cancel(self, task_id: str) -> TaskView:
        return TaskView.model_validate(self._request("POST", f"/api/tasks/{task_id}/cancel").json())

    def list_skills(self) -> list[dict]:
        return self._request("GET", "/api/skills").json()

    def wait(self, task_id: str, *, timeout: float = 300.0) -> TaskView:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        while True:
            task = self.get_task(task_id)
            if task.run is not None and task.run.status in TERMINAL_STATUSES:
                return task
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                message = f"Agent-X task {task_id} did not finish within {timeout:g} seconds"
                raise AgentXTimeoutError(message)
            time.sleep(min(self.poll_interval, remaining))

    def ask(self, input: str, *, selected_skills: list[str] | None = None,
            project_id: str = "default", conversation_id: str | None = None,
            idempotency_key: str | None = None, timeout: float = 300.0) -> str:
        submitted = self.submit(input, selected_skills=selected_skills, project_id=project_id,
                                conversation_id=conversation_id, idempotency_key=idempotency_key)
        completed = self.wait(submitted.task_id, timeout=timeout)
        if completed.run is None or completed.run.status != RunStatus.completed:
            raise AgentXTaskError(completed)
        return completed.run.final_answer or ""
