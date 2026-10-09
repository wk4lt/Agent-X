from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend.app import create_app
from contracts.models import RunStatus, RunView


class FakeHarnessClient:
    async def create_run(self, request):
        now = datetime.now(timezone.utc)
        return RunView(run_id=f"run_{request.task_id}", task_id=request.task_id, session_id=f"session_{request.task_id}",
                       status=RunStatus.accepted, created_at=now, updated_at=now)


def test_workspace_files_are_task_scoped_and_downloadable(tmp_path):
    workspace_root, log_root = tmp_path / "workspaces", tmp_path / "logs"
    app = create_app(client=FakeHarnessClient(), workspace_root=workspace_root, log_root=log_root)
    with TestClient(app) as client:
        first_view = client.post("/api/tasks", json={"input": "first"}).json()
        second = client.post("/api/tasks", json={"input": "second"}).json()["task_id"]
        first = first_view["task_id"]
        upload = client.post(f"/api/tasks/{first}/workspace/files", files={"file": ("notes.txt", b"private notes", "text/plain")})
        assert upload.status_code == 201
        assert upload.json()["name"] == "notes.txt"
        assert client.get(f"/api/tasks/{first}/workspace/files").json()[0]["size_bytes"] == 13
        assert client.get(f"/api/tasks/{second}/workspace/files").json() == []
        workspace_id = first_view["conversation_id"]
        assert (workspace_root / workspace_id / "notes.txt").read_bytes() == b"private notes"
        download = client.get(f"/api/tasks/{first}/workspace/files/notes.txt")
        assert download.status_code == 200
        assert download.content == b"private notes"
        audit = (log_root / f"session_{first}" / "backend.jsonl").read_text()
        assert "workspace.file_uploaded" in audit and "workspace.file_downloaded" in audit
