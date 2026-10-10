import pytest

from backend.config import HistoryConfigurationError, HistorySettings, bearer_api_key
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


def test_backend_api_keys_are_validated_and_compared(monkeypatch, tmp_path):
    first = "a" * 32
    second = "b" * 32
    monkeypatch.setenv("AGENT_API_KEYS", f"{first}, {second},{first}")
    settings = HistorySettings.from_environment(base_dir=tmp_path)
    assert settings.api_keys == (first, second)
    assert bearer_api_key(f"Bearer {second}", settings.api_keys) == second
    with pytest.raises(ValueError, match="invalid_api_key"):
        bearer_api_key("Bearer unknown", settings.api_keys)


def test_backend_rejects_short_api_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_API_KEYS", "too-short")
    with pytest.raises(HistoryConfigurationError, match="at least 32"):
        HistorySettings.from_environment(base_dir=tmp_path)
