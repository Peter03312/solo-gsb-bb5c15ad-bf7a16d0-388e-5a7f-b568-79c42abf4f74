"""Domain logic: over-temperature interval computation and diffing.

Rules
-----
* Readings of one device are ordered by (sampled_at, event_id).
* An over-temperature interval opens only when two *consecutive* valid
  readings both exceed the configured upper limit; the interval starts at
  the first of the two readings.
* The interval closes at the first subsequent reading that does not exceed
  the limit; while still open its end is null.
* An interval is identified by the event id of its start reading.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

Reading = dict[str, Any]
IntervalDict = dict[str, Any]


def to_epoch(dt: datetime) -> float:
    """Convert to Unix epoch seconds; naive datetimes are treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def to_utc_iso(dt: datetime) -> str:
    """Normalize a datetime to an ISO 8601 string in UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def compute_intervals(readings: list[Reading], limit: float) -> list[IntervalDict]:
    """Compute over-temperature intervals from ordered readings."""
    intervals: list[IntervalDict] = []
    prev: Reading | None = None
    open_start: Reading | None = None
    for reading in readings:
        exceeds = reading["temperature"] > limit
        if open_start is None:
            # Open only on the *second* consecutive exceedance; the start
            # point is the first reading of the exceeding pair.
            if exceeds and prev is not None and prev["temperature"] > limit:
                open_start = prev
        elif not exceeds:
            intervals.append(_make_interval(open_start, reading))
            open_start = None
        prev = reading
    if open_start is not None:
        intervals.append(_make_interval(open_start, None))
    return intervals


def _make_interval(start: Reading, end: Reading | None) -> IntervalDict:
    return {
        "start_event_id": start["event_id"],
        "start_sampled_at": start["sampled_at"],
        "start_epoch": start["sampled_at_epoch"],
        "end_event_id": end["event_id"] if end else None,
        "end_sampled_at": end["sampled_at"] if end else None,
        "end_epoch": end["sampled_at_epoch"] if end else None,
    }


def diff_intervals(
    old: list[IntervalDict], new: list[IntervalDict]
) -> tuple[list[IntervalDict], list[IntervalDict], list[IntervalDict]]:
    """Diff two interval sets keyed by start_event_id.

    A changed start point therefore shows up as one invalidated interval
    plus one added interval; a same-start interval with a different end
    shows up as changed.
    """
    old_by_start = {i["start_event_id"]: i for i in old}
    new_by_start = {i["start_event_id"]: i for i in new}
    added = [i for k, i in new_by_start.items() if k not in old_by_start]
    invalidated = [i for k, i in old_by_start.items() if k not in new_by_start]
    changed = [
        new_by_start[k]
        for k in new_by_start.keys() & old_by_start.keys()
        if new_by_start[k] != old_by_start[k]
    ]
    return added, changed, invalidated


# ---------------------------------------------------------------------------
# Persistence-backed helpers
# ---------------------------------------------------------------------------

def fetch_readings(conn: sqlite3.Connection, device_id: str) -> list[Reading]:
    rows = conn.execute(
        "SELECT event_id, sampled_at, sampled_at_epoch, temperature "
        "FROM readings WHERE device_id = ? "
        "ORDER BY sampled_at_epoch, event_id",
        (device_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_stored_intervals(conn: sqlite3.Connection, device_id: str) -> list[IntervalDict]:
    rows = conn.execute(
        "SELECT start_event_id, start_sampled_at, start_epoch, "
        "       end_event_id, end_sampled_at, end_epoch "
        "FROM intervals WHERE device_id = ? "
        "ORDER BY start_epoch, start_event_id",
        (device_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def recompute_intervals(
    conn: sqlite3.Connection, device_id: str, limit: float
) -> dict[str, list[IntervalDict]]:
    """Recompute all intervals of a device, persist them, and return the
    current set together with the added/changed/invalidated diff.

    Must be called inside a transaction (`with conn:`).
    """
    new = compute_intervals(fetch_readings(conn, device_id), limit)
    old = fetch_stored_intervals(conn, device_id)
    added, changed, invalidated = diff_intervals(old, new)
    conn.execute("DELETE FROM intervals WHERE device_id = ?", (device_id,))
    conn.executemany(
        "INSERT INTO intervals (device_id, start_event_id, start_sampled_at, "
        "start_epoch, end_event_id, end_sampled_at, end_epoch) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                device_id,
                i["start_event_id"],
                i["start_sampled_at"],
                i["start_epoch"],
                i["end_event_id"],
                i["end_sampled_at"],
                i["end_epoch"],
            )
            for i in new
        ],
    )
    return {
        "intervals": new,
        "added": added,
        "changed": changed,
        "invalidated": invalidated,
    }


def interval_to_api(interval: IntervalDict) -> dict[str, Any]:
    return {
        "start_event_id": interval["start_event_id"],
        "start_sampled_at": interval["start_sampled_at"],
        "end_event_id": interval["end_event_id"],
        "end_sampled_at": interval["end_sampled_at"],
    }
