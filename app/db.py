"""SQLite persistence helpers."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    device_id        TEXT NOT NULL,
    event_id         TEXT NOT NULL,
    sampled_at       TEXT NOT NULL,    -- ISO 8601, normalized to UTC
    sampled_at_epoch REAL NOT NULL,    -- sort key
    temperature      REAL NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (device_id, event_id)
);

CREATE TABLE IF NOT EXISTS intervals (
    device_id         TEXT NOT NULL,
    start_event_id    TEXT NOT NULL,   -- interval identifier
    start_sampled_at  TEXT NOT NULL,
    start_epoch       REAL NOT NULL,
    end_event_id      TEXT,            -- NULL while the interval is open
    end_sampled_at    TEXT,
    end_epoch         REAL,
    PRIMARY KEY (device_id, start_event_id)
);
"""

_initialized: set[str] = set()


def init_db(db_path: str) -> None:
    """Create tables if they do not exist yet."""
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open a connection, lazily initializing the schema on first use."""
    if db_path not in _initialized:
        init_db(db_path)
        _initialized.add(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
