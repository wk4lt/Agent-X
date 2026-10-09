from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from backend.config import HistorySettings


def run_migrations(settings: HistorySettings) -> None:
    try:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise RuntimeError(f"cannot create history database directory: {settings.database_path.parent}") from exc
    config = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    # Alembic runs synchronously; the application itself uses aiosqlite.
    config.set_main_option("sqlalchemy.url", f"sqlite:///{settings.database_path}")
    command.upgrade(config, "head")


if __name__ == "__main__":
    run_migrations(HistorySettings.from_environment())
