"""Typed runtime configuration. Secrets remain environment-only and are never logged."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from contracts.models import RunBudget


class ConfigurationError(ValueError):
    pass


def load_dotenv(path: Path = Path(".env")) -> None:
    """Small dependency-free dotenv reader; existing process variables always win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


def _integer(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = os.getenv(name, str(default))
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _seconds(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _ratio(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not 0 < value < 1:
        raise ConfigurationError(f"{name} must be between zero and one")
    return value


@dataclass(frozen=True)
class HarnessSettings:
    provider: str
    llm_base_url: str
    llm_api_key: str | None
    model: str
    llm_timeout_seconds: float
    context_window: int
    output_reserve: int
    context_safety_margin: int
    max_turns: int
    max_tool_calls: int
    run_wall_time_seconds: float
    tool_timeout_seconds: float
    tool_max_result_bytes: int
    tool_max_concurrency: int
    context_compaction_trigger_ratio: float
    context_compaction_target_ratio: float
    context_recent_blocks_to_keep: int
    context_tool_result_max_chars: int
    context_summary_max_input_tokens: int
    context_summary_max_output_tokens: int
    context_summary_timeout_seconds: float
    workspace_root: Path
    log_root: Path
    skill_project_root: Path
    skill_global_config_root: Path
    skill_script_policy: str
    skill_load_policy: str
    skill_allowed_extensions: tuple[str, ...]
    skill_default_timeout_seconds: float
    skill_max_timeout_seconds: float
    skill_max_output_bytes: int
    skill_max_concurrent_scripts_per_run: int

    @classmethod
    def from_environment(cls) -> "HarnessSettings":
        load_dotenv()
        key = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
        requested_provider = os.getenv("LLM_PROVIDER", "").strip().lower()
        provider = requested_provider or ("openai_compatible" if key else "mock")
        if provider not in {"mock", "openai_compatible"}:
            raise ConfigurationError("LLM_PROVIDER must be mock or openai_compatible")
        if provider == "openai_compatible" and not key:
            raise ConfigurationError("LLM_API_KEY or DEEPSEEK_API_KEY is required for openai_compatible")
        base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
        parsed = urlparse(base_url)
        if provider == "openai_compatible" and (parsed.scheme != "https" or not parsed.netloc):
            raise ConfigurationError("LLM_BASE_URL must be an https URL")
        context_window = _integer("CONTEXT_WINDOW", 128_000, minimum=1_024, maximum=1_000_000)
        output_reserve = _integer("OUTPUT_TOKEN_RESERVE", 8_192, minimum=1, maximum=384_000)
        safety_margin = _integer("CONTEXT_SAFETY_MARGIN", 1_024, minimum=0, maximum=100_000)
        if output_reserve + safety_margin >= context_window:
            raise ConfigurationError("OUTPUT_TOKEN_RESERVE + CONTEXT_SAFETY_MARGIN must be below CONTEXT_WINDOW")
        trigger = _ratio("CONTEXT_COMPACTION_TRIGGER_RATIO", 0.72)
        target = _ratio("CONTEXT_COMPACTION_TARGET_RATIO", 0.52)
        if target >= trigger:
            raise ConfigurationError("CONTEXT_COMPACTION_TARGET_RATIO must be below CONTEXT_COMPACTION_TRIGGER_RATIO")
        skill_policy = os.getenv("SKILL_SCRIPT_POLICY", "ask").lower()
        if skill_policy not in {"allow", "ask", "deny"}:
            raise ConfigurationError("SKILL_SCRIPT_POLICY must be allow, ask, or deny")
        skill_load_policy = os.getenv("SKILL_LOAD_POLICY", "allow").lower()
        if skill_load_policy not in {"allow", "deny"}:
            raise ConfigurationError("SKILL_LOAD_POLICY must be allow or deny")
        extensions = tuple(item.strip() for item in os.getenv("SKILL_ALLOWED_EXTENSIONS", ".py,.sh").split(",") if item.strip())
        if not extensions or any(item not in {".py", ".sh"} for item in extensions):
            raise ConfigurationError("SKILL_ALLOWED_EXTENSIONS only supports .py and .sh")
        return cls(
            provider=provider, llm_base_url=base_url, llm_api_key=key,
            model=os.getenv("LLM_MODEL", "deepseek-flash"),
            llm_timeout_seconds=_seconds("LLM_TIMEOUT_SECONDS", 90, minimum=1, maximum=600),
            context_window=context_window, output_reserve=output_reserve, context_safety_margin=safety_margin,
            max_turns=_integer("RUN_MAX_TURNS", 12, minimum=1, maximum=100),
            max_tool_calls=_integer("RUN_MAX_TOOL_CALLS", 32, minimum=0, maximum=500),
            run_wall_time_seconds=_seconds("RUN_WALL_TIME_SECONDS", 600, minimum=1, maximum=86_400),
            tool_timeout_seconds=_seconds("TOOL_TIMEOUT_SECONDS", 30, minimum=0.1, maximum=3_600),
            tool_max_result_bytes=_integer("TOOL_MAX_RESULT_BYTES", 32_768, minimum=256, maximum=10_485_760),
            tool_max_concurrency=_integer("TOOL_MAX_CONCURRENCY", 4, minimum=1, maximum=64),
            context_compaction_trigger_ratio=trigger,
            context_compaction_target_ratio=target,
            context_recent_blocks_to_keep=_integer("CONTEXT_RECENT_BLOCKS_TO_KEEP", 2, minimum=1, maximum=20),
            context_tool_result_max_chars=_integer("CONTEXT_TOOL_RESULT_MAX_CHARS", 4_000, minimum=128, maximum=1_000_000),
            context_summary_max_input_tokens=_integer("CONTEXT_SUMMARY_MAX_INPUT_TOKENS", 8_000, minimum=256, maximum=100_000),
            context_summary_max_output_tokens=_integer("CONTEXT_SUMMARY_MAX_OUTPUT_TOKENS", 800, minimum=64, maximum=20_000),
            context_summary_timeout_seconds=_seconds("CONTEXT_SUMMARY_TIMEOUT_SECONDS", 30, minimum=1, maximum=300),
            workspace_root=Path(os.getenv("WORKSPACE_ROOT", ".data/workspaces")).resolve(),
            log_root=Path(os.getenv("LOG_ROOT", ".data/logs")).resolve(),
            skill_project_root=Path(os.getenv("SKILL_PROJECT_ROOT", ".")).resolve(),
            skill_global_config_root=Path(os.getenv("SKILL_GLOBAL_CONFIG_ROOT", "~/.config")).expanduser().resolve(),
            skill_script_policy=skill_policy, skill_load_policy=skill_load_policy, skill_allowed_extensions=extensions,
            skill_default_timeout_seconds=_seconds("SKILL_SCRIPT_TIMEOUT_SECONDS", 30, minimum=1, maximum=120),
            skill_max_timeout_seconds=_seconds("SKILL_SCRIPT_MAX_TIMEOUT_SECONDS", 120, minimum=1, maximum=120),
            skill_max_output_bytes=_integer("SKILL_SCRIPT_MAX_OUTPUT_BYTES", 65_536, minimum=256, maximum=1_048_576),
            skill_max_concurrent_scripts_per_run=_integer("SKILL_MAX_CONCURRENT_SCRIPTS_PER_RUN", 2, minimum=1, maximum=16),
        )

    def default_budget(self) -> RunBudget:
        return RunBudget(max_turns=self.max_turns, max_tool_calls=self.max_tool_calls,
                         wall_time_seconds=self.run_wall_time_seconds, output_reserve=self.output_reserve,
                         context_window=self.context_window, safety_margin=self.context_safety_margin)
