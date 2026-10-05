"""Runtime configuration, overridable via environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_TEMP_UPPER_LIMIT = 8.0
DEFAULT_DB_PATH = "coldchain.db"


@dataclass(frozen=True)
class Settings:
    """Service settings.

    db_path:          SQLite database file path (env DB_PATH).
    temp_upper_limit: over-temperature threshold in Celsius; a reading is
                      "over limit" only when temperature > limit
                      (env TEMP_UPPER_LIMIT).
    """

    db_path: str = DEFAULT_DB_PATH
    temp_upper_limit: float = DEFAULT_TEMP_UPPER_LIMIT


def settings_from_env() -> Settings:
    raw_limit = os.environ.get("TEMP_UPPER_LIMIT", str(DEFAULT_TEMP_UPPER_LIMIT))
    try:
        limit = float(raw_limit)
    except ValueError as exc:
        raise ValueError(
            f"TEMP_UPPER_LIMIT must be a number, got {raw_limit!r}"
        ) from exc
    return Settings(
        db_path=os.environ.get("DB_PATH", DEFAULT_DB_PATH),
        temp_upper_limit=limit,
    )
