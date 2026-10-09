import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import HistorySettings
from backend.db import create_database
from backend.history import HistoryNotFound, HistoryRepository
from backend.migrate import run_migrations
from contracts.models import RunStatus, RunView, SessionEntry, ToolCall


class FakeHarness:
    def __init__(self) -> None:
        self.created = 0
        self.runs: dict[str, RunView] = {}

    async def create_run(self, request):
        self.created += 1
        now = datetime.now(timezone.utc)
        run = RunView(run_id=f"run_{self.created}", task_id=request.task_id, session_id=f"session_{self.created}",
                      status=RunStatus.accepted, created_at=now, updated_at=now)
        self.runs[run.run_id] = run
        return run

    async def get_run(self, run_id):
        return self.runs[run_id]


def settings_for(tmp_path) -> HistorySettings:
    return HistorySettings(database_path=tmp_path / "history" / "agent.db", cookie_secure=False)


def test_anonymous_cookie_history_isolated_and_task_retry_is_idempotent(tmp_path):
    harness = FakeHarness()
    app = create_app(client=harness, history_settings=settings_for(tmp_path), workspace_root=tmp_path / "work",
                     log_root=tmp_path / "logs")
    with TestClient(app) as browser:
        conversation = browser.post("/api/conversations", json={"title": "private"}).json()
        assert browser.get("/api/conversations").json()["items"][0]["id"] == conversation["id"]
        headers = {"Idempotency-Key": "browser-retry-1"}
        first = browser.post("/api/tasks", json={"conversation_id": conversation["id"], "input": "hello"}, headers=headers)
        second = browser.post("/api/tasks", json={"conversation_id": conversation["id"], "input": "hello"}, headers=headers)
        assert first.status_code == second.status_code == 202
        assert first.json()["task_id"] == second.json()["task_id"]
        assert harness.created == 1
        messages = browser.get(f"/api/conversations/{conversation['id']}/messages").json()["items"]
        assert [(message["role"], message["content"]) for message in messages] == [("user", "hello")]
        # Clearing the persistent cookie creates a different anonymous principal; IDs do not leak existence.
        browser.cookies.clear()
        assert browser.get(f"/api/conversations/{conversation['id']}/messages").status_code == 404
        assert browser.delete(f"/api/conversations/{conversation['id']}").status_code == 404
        assert browser.post(f"/api/tasks/{first.json()['task_id']}/cancel").status_code == 404


@pytest.mark.asyncio
async def test_migration_and_structured_messages_keep_tool_pair_and_raw_history(tmp_path):
    settings = settings_for(tmp_path)
    run_migrations(settings)
    assert settings.database_path.exists()
    engine, sessions = create_database(settings)
    repository = HistoryRepository(sessions, settings)
    principal, token = await repository.resolve_anonymous(None)
    same_principal, replacement = await repository.resolve_anonymous(token)
    assert same_principal == principal and replacement is None
    conversation = await repository.create_conversation(principal, "tool history")
    await repository.create_run_mapping(principal_id=principal, conversation_id=conversation.id, task_id="task_1",
                                        run_id="run_1", session_id="session_1", project_id="default", user_input="look up")
    call = ToolCall(id="call_1", name="lookup", arguments={"term": "x"})
    entries = [
        SessionEntry(id="entry_call", role="assistant", tool_calls=[call]),
        SessionEntry(id="entry_result", role="tool", content="result", tool_call_id="call_1", tool_name="lookup"),
        SessionEntry(id="entry_answer", role="assistant", content="done"),
    ]
    await repository.append_harness_entries(principal, "run_1", entries)
    await repository.append_harness_entries(principal, "run_1", entries)  # SSE reconnect is idempotent.
    page = await repository.list_messages(principal, conversation.id, None)
    assert [item.sequence for item in page.items] == [1, 2, 3, 4]
    assert page.items[1].tool_calls[0].id == page.items[2].tool_call_id == "call_1"
    assert page.items[-1].content == "done"
    await asyncio.gather(
        repository.create_run_mapping(principal_id=principal, conversation_id=conversation.id, task_id="task_2",
                                      run_id="run_2", session_id="session_2", project_id="default", user_input="two"),
        repository.create_run_mapping(principal_id=principal, conversation_id=conversation.id, task_id="task_3",
                                      run_id="run_3", session_id="session_3", project_id="default", user_input="three"),
    )
    concurrent_page = await repository.list_messages(principal, conversation.id, None)
    assert [item.sequence for item in concurrent_page.items] == list(range(1, 7))
    other, _ = await repository.resolve_anonymous(None)
    with pytest.raises(HistoryNotFound):
        await repository.list_messages(other, conversation.id, None)
    await engine.dispose()
