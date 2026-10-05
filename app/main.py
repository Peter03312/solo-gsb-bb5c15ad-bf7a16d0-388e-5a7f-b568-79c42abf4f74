"""HTTP API for the cold-chain temperature event verification service."""
from __future__ import annotations

import math
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import service
from .config import Settings, settings_from_env
from .db import connect, init_db
from .models import CorrectionIn, Interval, MutationResult, ReadingIn, ReadingOut


def _json_safe(value: Any) -> Any:
    """Make a structure strictly JSON-serializable (no NaN/Infinity)."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(settings.db_path)
        yield

    app = FastAPI(
        title="Cold-Chain Temperature Event Verification Service",
        version="1.0.0",
        summary="Verify over-temperature intervals from per-device readings.",
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_json_safe(
                {
                    "message": "invalid request: malformed event id, timestamp "
                               "or temperature",
                    "detail": jsonable_encoder(exc.errors()),
                }
            ),
        )

    def _mutation_result(
        device_id: str, result: dict[str, list], duplicate: bool = False
    ) -> MutationResult:
        return MutationResult(
            device_id=device_id,
            duplicate=duplicate,
            intervals=[service.interval_to_api(i) for i in result["intervals"]],
            added=[service.interval_to_api(i) for i in result["added"]],
            changed=[service.interval_to_api(i) for i in result["changed"]],
            invalidated=[service.interval_to_api(i) for i in result["invalidated"]],
        )

    # ------------------------------------------------------------------
    # Readings
    # ------------------------------------------------------------------

    @app.post(
        "/api/devices/{device_id}/readings",
        response_model=MutationResult,
        tags=["readings"],
        summary="Submit a reading (idempotent per event_id)",
    )
    def submit_reading(device_id: str, body: ReadingIn, response: Response):
        epoch = service.to_epoch(body.sampled_at)
        sampled_at = service.to_utc_iso(body.sampled_at)
        with connect(settings.db_path) as conn:
            existing = conn.execute(
                "SELECT sampled_at_epoch, temperature FROM readings "
                "WHERE device_id = ? AND event_id = ?",
                (device_id, body.event_id),
            ).fetchone()
            if existing is not None:
                same_content = (
                    existing["sampled_at_epoch"] == epoch
                    and existing["temperature"] == body.temperature
                )
                if same_content:
                    # Idempotent replay: no state change, empty diff.
                    response.status_code = 200
                    return _mutation_result(
                        device_id,
                        {
                            "intervals": service.fetch_stored_intervals(conn, device_id),
                            "added": [],
                            "changed": [],
                            "invalidated": [],
                        },
                        duplicate=True,
                    )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"event '{body.event_id}' already exists for device "
                        f"'{device_id}' with different content; use PUT to "
                        "correct it or DELETE to revoke it"
                    ),
                )
            now = service.utc_now_iso()
            with conn:
                conn.execute(
                    "INSERT INTO readings (device_id, event_id, sampled_at, "
                    "sampled_at_epoch, temperature, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (device_id, body.event_id, sampled_at, epoch,
                     body.temperature, now, now),
                )
                result = service.recompute_intervals(
                    conn, device_id, settings.temp_upper_limit
                )
            response.status_code = 201
            return _mutation_result(device_id, result)

    @app.put(
        "/api/devices/{device_id}/readings/{event_id}",
        response_model=MutationResult,
        tags=["readings"],
        summary="Correct an existing reading by event id",
    )
    def correct_reading(device_id: str, event_id: str, body: CorrectionIn):
        with connect(settings.db_path) as conn:
            existing = conn.execute(
                "SELECT sampled_at_epoch, temperature FROM readings "
                "WHERE device_id = ? AND event_id = ?",
                (device_id, event_id),
            ).fetchone()
            if existing is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"cannot correct: event '{event_id}' does not exist "
                        f"for device '{device_id}'"
                    ),
                )
            epoch = (
                service.to_epoch(body.sampled_at)
                if body.sampled_at is not None
                else existing["sampled_at_epoch"]
            )
            sampled_at = (
                service.to_utc_iso(body.sampled_at)
                if body.sampled_at is not None
                else None
            )
            temperature = (
                body.temperature
                if body.temperature is not None
                else existing["temperature"]
            )
            with conn:
                conn.execute(
                    "UPDATE readings SET sampled_at_epoch = ?, temperature = ?, "
                    "sampled_at = COALESCE(?, sampled_at), updated_at = ? "
                    "WHERE device_id = ? AND event_id = ?",
                    (epoch, temperature, sampled_at, service.utc_now_iso(),
                     device_id, event_id),
                )
                result = service.recompute_intervals(
                    conn, device_id, settings.temp_upper_limit
                )
            return _mutation_result(device_id, result)

    @app.delete(
        "/api/devices/{device_id}/readings/{event_id}",
        response_model=MutationResult,
        tags=["readings"],
        summary="Revoke a reading by event id",
    )
    def revoke_reading(device_id: str, event_id: str):
        with connect(settings.db_path) as conn:
            existing = conn.execute(
                "SELECT 1 FROM readings WHERE device_id = ? AND event_id = ?",
                (device_id, event_id),
            ).fetchone()
            if existing is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"cannot revoke: event '{event_id}' does not exist "
                        f"for device '{device_id}'"
                    ),
                )
            with conn:
                conn.execute(
                    "DELETE FROM readings WHERE device_id = ? AND event_id = ?",
                    (device_id, event_id),
                )
                result = service.recompute_intervals(
                    conn, device_id, settings.temp_upper_limit
                )
            return _mutation_result(device_id, result)

    @app.get(
        "/api/devices/{device_id}/readings",
        response_model=list[ReadingOut],
        tags=["readings"],
        summary="List readings ordered by (sampled_at, event_id)",
    )
    def list_readings(device_id: str):
        with connect(settings.db_path) as conn:
            readings = service.fetch_readings(conn, device_id)
        return [
            ReadingOut(
                event_id=r["event_id"],
                sampled_at=r["sampled_at"],
                temperature=r["temperature"],
            )
            for r in readings
        ]

    # ------------------------------------------------------------------
    # Intervals
    # ------------------------------------------------------------------

    @app.get(
        "/api/devices/{device_id}/intervals",
        response_model=list[Interval],
        tags=["intervals"],
        summary="List current over-temperature intervals of a device",
    )
    def list_intervals(device_id: str):
        with connect(settings.db_path) as conn:
            stored = service.fetch_stored_intervals(conn, device_id)
        return [service.interval_to_api(i) for i in stored]

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    @app.get("/api/health", tags=["meta"], summary="Health check and active config")
    def health():
        return {
            "status": "ok",
            "temp_upper_limit": settings.temp_upper_limit,
        }

    return app


app = create_app(settings_from_env())
