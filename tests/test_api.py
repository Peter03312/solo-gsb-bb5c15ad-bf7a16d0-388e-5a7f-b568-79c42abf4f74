"""End-to-end API tests for the cold-chain verification service."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

LIMIT = 8.0


@pytest.fixture()
def client(tmp_path):
    settings = Settings(db_path=str(tmp_path / "test.db"), temp_upper_limit=LIMIT)
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def submit(client, event_id, sampled_at, temperature, device="truck-01"):
    return client.post(
        f"/api/devices/{device}/readings",
        json={
            "event_id": event_id,
            "sampled_at": sampled_at,
            "temperature": temperature,
        },
    )


# ---------------------------------------------------------------------------
# Interval formation
# ---------------------------------------------------------------------------

def test_single_exceedance_forms_no_interval(client):
    r = submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    assert r.status_code == 201
    body = r.json()
    assert body["intervals"] == []
    assert body["added"] == []


def test_two_consecutive_exceedances_open_interval(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    r = submit(client, "e2", "2026-10-01T08:10:00Z", 9.5)
    body = r.json()
    assert len(body["added"]) == 1
    interval = body["added"][0]
    assert interval["start_event_id"] == "e1"
    assert interval["start_sampled_at"] == "2026-10-01T08:00:00Z"
    assert interval["end_event_id"] is None
    assert interval["end_sampled_at"] is None
    assert body["intervals"] == body["added"]


def test_interval_closes_on_first_normal_reading(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    submit(client, "e2", "2026-10-01T08:10:00Z", 9.5)
    r = submit(client, "e3", "2026-10-01T08:20:00Z", 4.0)
    body = r.json()
    assert body["added"] == []
    assert len(body["changed"]) == 1
    changed = body["changed"][0]
    assert changed["start_event_id"] == "e1"
    assert changed["end_event_id"] == "e3"
    assert changed["end_sampled_at"] == "2026-10-01T08:20:00Z"
    assert body["invalidated"] == []


def test_temperature_equal_to_limit_is_not_over(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", LIMIT)
    r = submit(client, "e2", "2026-10-01T08:10:00Z", LIMIT)
    assert r.json()["intervals"] == []


def test_non_consecutive_exceedances_form_no_interval(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    submit(client, "e2", "2026-10-01T08:10:00Z", 4.0)
    r = submit(client, "e3", "2026-10-01T08:20:00Z", 9.5)
    assert r.json()["intervals"] == []


def test_multiple_intervals(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    submit(client, "e2", "2026-10-01T08:10:00Z", 9.5)   # interval e1 opens
    submit(client, "e3", "2026-10-01T08:20:00Z", 4.0)   # interval e1 closes
    submit(client, "e4", "2026-10-01T08:30:00Z", 10.0)
    r = submit(client, "e5", "2026-10-01T08:40:00Z", 10.5)  # interval e4 opens
    intervals = r.json()["intervals"]
    assert [i["start_event_id"] for i in intervals] == ["e1", "e4"]
    assert intervals[0]["end_event_id"] == "e3"
    assert intervals[1]["end_event_id"] is None
    assert [i["start_event_id"] for i in r.json()["added"]] == ["e4"]


# ---------------------------------------------------------------------------
# Late / corrected / revoked readings trigger recomputation
# ---------------------------------------------------------------------------

def test_late_normal_reading_invalidates_interval(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    submit(client, "e3", "2026-10-01T08:20:00Z", 9.5)  # interval e1 opens
    # Late reading with normal temperature lands between the two exceedances.
    r = submit(client, "e2", "2026-10-01T08:10:00Z", 4.0)
    body = r.json()
    assert body["intervals"] == []
    assert [i["start_event_id"] for i in body["invalidated"]] == ["e1"]
    assert body["added"] == []


def test_late_normal_reading_closes_open_interval(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    submit(client, "e2", "2026-10-01T08:10:00Z", 9.2)
    submit(client, "e4", "2026-10-01T08:30:00Z", 9.4)  # interval e1 still open
    r = submit(client, "e3", "2026-10-01T08:20:00Z", 3.0)  # late, normal
    body = r.json()
    assert [i["start_event_id"] for i in body["changed"]] == ["e1"]
    assert body["intervals"][0]["end_event_id"] == "e3"


def test_correction_of_temperature_moves_interval_start(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    submit(client, "e2", "2026-10-01T08:10:00Z", 9.2)
    submit(client, "e3", "2026-10-01T08:20:00Z", 9.4)  # interval starts at e1
    r = client.put("/api/devices/truck-01/readings/e1", json={"temperature": 2.0})
    assert r.status_code == 200
    body = r.json()
    # Start moved e1 -> e2: old interval invalidated, new one added.
    assert [i["start_event_id"] for i in body["invalidated"]] == ["e1"]
    assert [i["start_event_id"] for i in body["added"]] == ["e2"]
    assert [i["start_event_id"] for i in body["intervals"]] == ["e2"]


def test_correction_of_sampled_at_reorders_and_recomputes(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    submit(client, "e2", "2026-10-01T08:10:00Z", 9.2)  # interval starts at e1
    r = client.put(
        "/api/devices/truck-01/readings/e2",
        json={"sampled_at": "2026-10-01T07:50:00Z"},
    )
    body = r.json()
    # Order is now e2 (07:50), e1 (08:00): interval starts at e2.
    assert [i["start_event_id"] for i in body["invalidated"]] == ["e1"]
    assert [i["start_event_id"] for i in body["added"]] == ["e2"]


def test_correction_to_normal_temperature_invalidates_interval(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    submit(client, "e2", "2026-10-01T08:10:00Z", 9.2)
    r = client.put("/api/devices/truck-01/readings/e2", json={"temperature": 5.0})
    body = r.json()
    assert body["intervals"] == []
    assert [i["start_event_id"] for i in body["invalidated"]] == ["e1"]


def test_revoke_reading_recomputes_intervals(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    submit(client, "e2", "2026-10-01T08:10:00Z", 9.2)
    r = client.delete("/api/devices/truck-01/readings/e2")
    assert r.status_code == 200
    body = r.json()
    assert body["intervals"] == []
    assert [i["start_event_id"] for i in body["invalidated"]] == ["e1"]
    readings = client.get("/api/devices/truck-01/readings").json()
    assert [r["event_id"] for r in readings] == ["e1"]


def test_revoke_normal_reading_can_open_interval(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    submit(client, "e2", "2026-10-01T08:10:00Z", 4.0)  # breaks consecutiveness
    submit(client, "e3", "2026-10-01T08:20:00Z", 9.5)
    assert client.get("/api/devices/truck-01/intervals").json() == []
    r = client.delete("/api/devices/truck-01/readings/e2")
    body = r.json()
    assert [i["start_event_id"] for i in body["added"]] == ["e1"]
    assert body["intervals"][0]["end_event_id"] is None


# ---------------------------------------------------------------------------
# Idempotency and conflicts
# ---------------------------------------------------------------------------

def test_duplicate_submission_is_idempotent(client):
    payload = {
        "event_id": "e1",
        "sampled_at": "2026-10-01T08:00:00Z",
        "temperature": 9.0,
    }
    r1 = client.post("/api/devices/truck-01/readings", json=payload)
    assert r1.status_code == 201
    r2 = client.post("/api/devices/truck-01/readings", json=payload)
    assert r2.status_code == 200
    body = r2.json()
    assert body["duplicate"] is True
    assert body["added"] == [] and body["changed"] == [] and body["invalidated"] == []
    readings = client.get("/api/devices/truck-01/readings").json()
    assert len(readings) == 1


def test_same_event_id_with_different_content_is_rejected(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    r = submit(client, "e1", "2026-10-01T08:00:00Z", 3.0)   # different temperature
    assert r.status_code == 409
    r = submit(client, "e1", "2026-10-01T08:05:00Z", 9.0)   # different timestamp
    assert r.status_code == 409
    assert "e1" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

def test_invalid_sampled_at_rejected(client):
    r = submit(client, "e1", "not-a-timestamp", 9.0)
    assert r.status_code == 422
    r = submit(client, "e1", "2026-13-40T99:99:99Z", 9.0)
    assert r.status_code == 422


def test_invalid_temperature_rejected(client):
    r = submit(client, "e1", "2026-10-01T08:00:00Z", "hot")
    assert r.status_code == 422
    r = submit(client, "e1", "2026-10-01T08:00:00Z", -500.0)
    assert r.status_code == 422
    # NaN / Infinity are not valid temperatures (send raw JSON: httpx's
    # json= shortcut refuses non-finite floats client-side).
    for bad in ("NaN", "Infinity", "-Infinity"):
        r = client.post(
            "/api/devices/truck-01/readings",
            content=(
                '{"event_id": "e1", "sampled_at": "2026-10-01T08:00:00Z", '
                f'"temperature": {bad}}}'
            ),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 422, bad


def test_missing_or_empty_fields_rejected(client):
    r = client.post("/api/devices/truck-01/readings", json={"event_id": "e1"})
    assert r.status_code == 422
    r = submit(client, "", "2026-10-01T08:00:00Z", 9.0)
    assert r.status_code == 422


def test_correct_or_revoke_missing_event_returns_404(client):
    r = client.put("/api/devices/truck-01/readings/nope", json={"temperature": 5.0})
    assert r.status_code == 404
    r = client.delete("/api/devices/truck-01/readings/nope")
    assert r.status_code == 404


def test_empty_correction_rejected(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    r = client.put("/api/devices/truck-01/readings/e1", json={})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Ordering, isolation, normalization
# ---------------------------------------------------------------------------

def test_readings_sorted_by_sampled_at_then_event_id(client):
    submit(client, "e2", "2026-10-01T08:10:00Z", 5.0)
    submit(client, "e1", "2026-10-01T08:00:00Z", 5.0)
    submit(client, "e0", "2026-10-01T08:00:00Z", 5.0)  # same time, smaller id
    readings = client.get("/api/devices/truck-01/readings").json()
    assert [r["event_id"] for r in readings] == ["e0", "e1", "e2"]


def test_devices_are_isolated(client):
    submit(client, "e1", "2026-10-01T08:00:00Z", 9.0)
    submit(client, "e2", "2026-10-01T08:10:00Z", 9.0)
    assert client.get("/api/devices/truck-02/intervals").json() == []
    # Same event id on another device is an independent event.
    r = submit(client, "e1", "2026-10-01T08:00:00Z", 1.0, device="truck-02")
    assert r.status_code == 201


def test_timezone_offsets_are_normalized_to_utc(client):
    submit(client, "e1", "2026-10-01T16:00:00+08:00", 9.0)
    readings = client.get("/api/devices/truck-01/readings").json()
    assert readings[0]["sampled_at"] == "2026-10-01T08:00:00Z"


def test_health_reports_configured_limit(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "temp_upper_limit": LIMIT}
