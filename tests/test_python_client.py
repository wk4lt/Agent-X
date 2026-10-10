from datetime import datetime, timezone

import httpx

from agent_x import AgentXAPIError, AgentXClient
from contracts.models import RunStatus, RunView, TaskView


def task_payload(status: RunStatus, *, final_answer: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    run = RunView(run_id="run_1", task_id="task_1", session_id="session_1", status=status,
                  final_answer=final_answer, created_at=now, updated_at=now)
    return TaskView(task_id="task_1", project_id="default", input="hello", conversation_id="conv_1",
                    run=run).model_dump(mode="json")


def test_ask_submits_with_auth_and_waits_for_answer():
    requests: list[httpx.Request] = []
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(202, json=task_payload(RunStatus.accepted))
        polls += 1
        status = RunStatus.running if polls == 1 else RunStatus.completed
        answer = "done" if status == RunStatus.completed else None
        return httpx.Response(200, json=task_payload(status, final_answer=answer))

    http = httpx.Client(base_url="https://agent-x.test", transport=httpx.MockTransport(handler))
    with AgentXClient(
        "https://agent-x.test", api_key="secret", poll_interval=0.001, client=http,
    ) as client:
        assert client.ask("hello", selected_skills=["code-doc"], timeout=1) == "done"

    assert requests[0].headers["authorization"] == "Bearer secret"
    assert requests[0].headers["idempotency-key"]
    assert requests[0].read()
    http.close()


def test_api_error_exposes_status_and_detail():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid_api_key"})

    http = httpx.Client(base_url="https://agent-x.test", transport=httpx.MockTransport(handler))
    client = AgentXClient("https://agent-x.test", client=http)
    try:
        client.list_skills()
        raise AssertionError("expected AgentXAPIError")
    except AgentXAPIError as exc:
        assert exc.status_code == 401
        assert exc.detail == "invalid_api_key"
    finally:
        http.close()
