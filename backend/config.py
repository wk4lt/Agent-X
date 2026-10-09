"""Backend configuration for durable, local conversation history."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class HistoryConfigurationError(RuntimeError):
    pass


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Read local development configuration without ever overriding deployment env vars."""
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


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw.lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.lower() in {"0", "false", "no", "off"}:
        return False
    raise HistoryConfigurationError(f"{name} must be a boolean")


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError as exc:
        raise HistoryConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise HistoryConfigurationError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class HistorySettings:
    database_path: Path
    busy_timeout_ms: int = 5_000
    cookie_name: str = "agent_anon"
    cookie_max_age_days: int = 30
    cookie_secure: bool = False
    cookie_http_only: bool = True
    cookie_same_site: str = "lax"

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path}"

    @classmethod
    def from_environment(cls, *, base_dir: Path | None = None) -> "HistorySettings":
        _load_dotenv()
        root = (base_dir or Path.cwd()).resolve()
        configured = Path(os.getenv("HISTORY_DB_PATH", ".data/agent_history.db"))
        path = configured if configured.is_absolute() else (root / configured)
        path = path.resolve()
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.parent.chmod(0o700)
            # Validate the configured location without silently selecting another one.
            with path.parent.joinpath(".history-write-check").open("a", encoding="utf-8"):
                pass
            path.parent.joinpath(".history-write-check").unlink(missing_ok=True)
        except OSError as exc:
            raise HistoryConfigurationError(
                f"history database directory is not usable: {path.parent} ({exc})"
            ) from exc
        same_site = os.getenv("ANON_COOKIE_SAME_SITE", "lax").lower()
        if same_site not in {"lax", "strict", "none"}:
            raise HistoryConfigurationError("ANON_COOKIE_SAME_SITE must be lax, strict, or none")
        return cls(
            database_path=path,
            busy_timeout_ms=_positive_int("HISTORY_BUSY_TIMEOUT_MS", 5_000),
            cookie_name=os.getenv("ANON_COOKIE_NAME", "agent_anon"),
            cookie_max_age_days=_positive_int("ANON_COOKIE_MAX_AGE_DAYS", 30),
            cookie_secure=_bool("ANON_COOKIE_SECURE", False),
            cookie_http_only=_bool("ANON_COOKIE_HTTP_ONLY", True),
            cookie_same_site=same_site,
        )
