from dataclasses import replace

import httpx
from fastapi.testclient import TestClient

from backend.app import create_app as create_backend
from backend.config import HistorySettings
from harness.app import create_app as create_harness
from harness.config import HarnessSettings
from harness.provider import ScriptedMockProvider


def test_skill_preview_returns_full_file_without_running_scripts(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_INTERNAL_TOKEN", "preview-test")
    skill = tmp_path / "skills/demo"
    skill.mkdir(parents=True)
    content = "---\nname: demo\ndescription: Preview demo\n---\n# Full instructions\n" + "Detail\n" * 1000
    (skill / "SKILL.md").write_text(content)
    scripts = skill / "scripts"
    scripts.mkdir()
    (scripts / "fail.py").write_text("raise RuntimeError('must not execute')")
    settings = replace(HarnessSettings.from_environment(), skill_project_root=tmp_path)
    headers = {"X-Internal-Token": "preview-test"}
    with TestClient(create_harness(settings=settings, provider=ScriptedMockProvider([]))) as browser:
        assert browser.get("/internal/skills/demo").status_code == 401
        response = browser.get("/internal/skills/demo", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"name": "demo", "content": content}
        assert browser.get("/internal/skills/missing", headers=headers).status_code == 404
        assert browser.get("/internal/skills/SKILL.md", headers=headers).status_code == 404
    denied = replace(settings, skill_load_policy="deny")
    with TestClient(create_harness(settings=denied, provider=ScriptedMockProvider([]))) as browser:
        assert browser.get("/internal/skills", headers=headers).json() == []
        assert browser.get("/internal/skills/demo", headers=headers).status_code == 403


def test_backend_preview_proxies_content_and_preserves_unavailable_status(tmp_path):
    class PreviewHarness:
        async def skill_content(self, name):
            if name != "demo":
                status = 403 if name == "denied" else 404 if name == "missing" else 500
                response = httpx.Response(status, request=httpx.Request("GET", "http://harness/internal/skills/test"))
                response.raise_for_status()
            return {"name": name, "content": "---\n# Instructions\n", "skill_root": "/private/path"}

    app = create_backend(client=PreviewHarness(),
                         history_settings=HistorySettings(database_path=tmp_path / "history.db", cookie_secure=False),
                         workspace_root=tmp_path / "work", log_root=tmp_path / "logs")
    with TestClient(app) as browser:
        assert browser.get("/api/skills/demo").json() == {"name": "demo", "content": "---\n# Instructions\n"}
        assert browser.get("/api/skills/missing").status_code == 404
        assert browser.get("/api/skills/denied").status_code == 403
        assert browser.get("/api/skills/broken").status_code == 502
