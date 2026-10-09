import pytest

from contracts.models import CreateRunRequest
from harness.config import ConfigurationError, HarnessSettings


def test_settings_rejects_context_that_cannot_fit_output(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CONTEXT_WINDOW", "1024")
    monkeypatch.setenv("OUTPUT_TOKEN_RESERVE", "900")
    monkeypatch.setenv("CONTEXT_SAFETY_MARGIN", "200")
    with pytest.raises(ConfigurationError, match="below CONTEXT_WINDOW"):
        HarnessSettings.from_environment()


def test_settings_exposes_tool_and_context_limits(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CONTEXT_WINDOW", "8000")
    monkeypatch.setenv("OUTPUT_TOKEN_RESERVE", "1000")
    monkeypatch.setenv("CONTEXT_SAFETY_MARGIN", "200")
    monkeypatch.setenv("TOOL_MAX_RESULT_BYTES", "4096")
    settings = HarnessSettings.from_environment()
    assert settings.default_budget().context_window == 8000
    assert settings.tool_max_result_bytes == 4096


def test_internal_run_payload_does_not_override_harness_defaults():
    request = CreateRunRequest(task_id="task_1", idempotency_key="request_1", input="hello")
    payload = request.model_dump(mode="json", exclude_unset=True)
    assert "model" not in payload
    assert "budget" not in payload
