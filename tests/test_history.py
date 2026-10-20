import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import httpx
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import HistorySettings
from backend.db import Conversation, create_database
from backend.history import HistoryNotFound, HistoryRepository
from backend.migrate import run_migrations
from backend.workspace_cleanup import prune_expired_workspaces
from contracts.models import RunStatus, RunView, SessionEntry, ToolCall


class FakeHarness:
    def __init__(self) -> None:
        self.created = 0
        self.runs: dict[str, RunView] = {}
        self.requests = []

    async def create_run(self, request):
        self.created += 1
        self.requests.append(request)
        now = datetime.now(timezone.utc)
        run = RunView(run_id=f"run_{self.created}", task_id=request.task_id,
                      session_id=request.session_id or f"session_{self.created}",
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


def test_follow_up_reuses_the_conversation_harness_session(tmp_path):
    harness = FakeHarness()
    app = create_app(client=harness, history_settings=settings_for(tmp_path), workspace_root=tmp_path / "work",
                     log_root=tmp_path / "logs")
    with TestClient(app) as browser:
        conversation = browser.post("/api/conversations", json={"title": "continuity"}).json()
        first = browser.post("/api/tasks", json={"conversation_id": conversation["id"], "input": "记住：代号是青竹"})
        second = browser.post("/api/tasks", json={"conversation_id": conversation["id"], "input": "代号是什么？"})

    assert first.status_code == second.status_code == 202
    assert harness.requests[0].session_id is None
    assert harness.requests[1].session_id == first.json()["run"]["session_id"]
    assert second.json()["run"]["session_id"] == first.json()["run"]["session_id"]


def test_log_identifiers_survive_unavailable_harness(tmp_path, monkeypatch):
    harness = FakeHarness()
    app = create_app(client=harness, history_settings=settings_for(tmp_path), workspace_root=tmp_path / "work",
                     log_root=tmp_path / "logs")
    with TestClient(app) as browser:
        created = browser.post("/api/tasks", json={"input": "find this run in logs"}).json()
        session_id, run_id = created["run"]["session_id"], created["run"]["run_id"]
        assert created["session_id"] == session_id and created["run_id"] == run_id

        async def missing_run(_run_id):
            raise httpx.ConnectError("harness restarted")

        monkeypatch.setattr(harness, "get_run", missing_run)
        historical = browser.get(f"/api/conversations/{created['conversation_id']}/latest-task").json()
        listed = browser.get("/api/tasks").json()[0]
        for task in (historical, listed):
            assert task["run"] is None
            assert task["session_id"] == session_id and task["run_id"] == run_id
            assert task["task_id"] == created["task_id"]


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


@pytest.mark.asyncio
async def test_workspace_retention_removes_only_expired_conversation_directories(tmp_path):
    settings = settings_for(tmp_path)
    run_migrations(settings)
    engine, sessions = create_database(settings)
    repository = HistoryRepository(sessions, settings)
    principal, _ = await repository.resolve_anonymous(None)
    expired = await repository.create_conversation(principal, "expired")
    current = await repository.create_conversation(principal, "current")
    now = datetime.now(timezone.utc)
    async with sessions.begin() as session:
        row = await session.get(Conversation, expired.id)
        assert row is not None
        row.updated_at = now - timedelta(days=31)
    workspace_root = tmp_path / "workspaces"
    (workspace_root / expired.id).mkdir(parents=True)
    (workspace_root / expired.id / "artifact.txt").write_text("old", encoding="utf-8")
    (workspace_root / current.id).mkdir()
    (workspace_root / current.id / "artifact.txt").write_text("current", encoding="utf-8")

    removed = await prune_expired_workspaces(repository, workspace_root, retention_days=30, now=now)

    assert removed == [expired.id]
    assert not (workspace_root / expired.id).exists()
    assert (workspace_root / current.id / "artifact.txt").read_text(encoding="utf-8") == "current"
    await engine.dispose()
