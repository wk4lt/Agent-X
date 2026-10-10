from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import HistorySettings
from contracts.models import RunStatus, RunView


class FakeHarness:
    def __init__(self) -> None:
        self.runs: dict[str, RunView] = {}

    async def create_run(self, request):
        now = datetime.now(timezone.utc)
        run = RunView(run_id=f"run_{request.task_id}", task_id=request.task_id,
                      session_id=f"session_{request.task_id}", status=RunStatus.accepted,
                      created_at=now, updated_at=now)
        self.runs[run.run_id] = run
        return run

    async def get_run(self, run_id):
        return self.runs[run_id]


def test_bearer_key_has_stable_isolated_identity(tmp_path):
    api_key = "api-key-that-is-long-enough-for-testing"
    settings = HistorySettings(database_path=tmp_path / "history" / "agent.db", api_keys=(api_key,))
    app = create_app(
        client=FakeHarness(), history_settings=settings, workspace_root=tmp_path / "work",
        log_root=tmp_path / "logs",
    )
    authorization = {"Authorization": f"Bearer {api_key}"}

    with TestClient(app) as first:
        created = first.post("/api/tasks", json={"input": "from script"}, headers=authorization)
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        assert settings.cookie_name not in created.cookies

    # A fresh HTTP client can still access resources owned by the same API key.
    with TestClient(app) as second:
        assert second.get(f"/api/tasks/{task_id}", headers=authorization).status_code == 200
        assert second.get(f"/api/tasks/{task_id}").status_code == 404


def test_invalid_or_malformed_bearer_is_rejected(tmp_path):
    api_key = "api-key-that-is-long-enough-for-testing"
    settings = HistorySettings(database_path=tmp_path / "history" / "agent.db", api_keys=(api_key,))
    app = create_app(
        client=FakeHarness(), history_settings=settings, workspace_root=tmp_path / "work",
        log_root=tmp_path / "logs",
    )

    with TestClient(app) as client:
        invalid = client.get("/api/tasks", headers={"Authorization": "Bearer wrong"})
        malformed = client.get("/api/tasks", headers={"Authorization": api_key})
        assert invalid.status_code == malformed.status_code == 401
        assert invalid.headers["www-authenticate"] == "Bearer"
