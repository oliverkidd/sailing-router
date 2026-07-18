"""/wind/area — multi-hour display series for viewport overlays + playback."""

from __future__ import annotations

import hashlib
import json
import queue
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import StreamingResponse

from ..config import WIND_TTL_S
from ..field_service import get_display_series
from ..forecast.grib_source import ForecastRateLimitError
from ..schemas import BBox
from ..util.timeparse import parse_utc_iso

router = APIRouter(tags=["forecast"])

_SENTINEL = object()


def _parse_window(
    lat_min: float,
    lon_min: float,
    lat_max: float,
    lon_max: float,
    start_utc: str | None,
    end_utc: str | None,
) -> tuple[BBox, datetime, datetime]:
    bbox = BBox(lat_min=lat_min, lon_min=lon_min, lat_max=lat_max, lon_max=lon_max)
    now = datetime.now(UTC)
    start = parse_utc_iso(start_utc) if start_utc else now
    end = parse_utc_iso(end_utc) if end_utc else start + timedelta(hours=24)
    return bbox, start, end


@router.get("/wind/area")
def wind_area(
    response: Response,
    lat_min: float,
    lon_min: float,
    lat_max: float,
    lon_max: float,
    model_id: str = "ecmwf",
    start_utc: str | None = None,
    end_utc: str | None = None,
    zoom: float | None = None,
    terrain: bool = True,
) -> dict:
    """Return a multi-hour wind series for the viewport (WindProvider path).

    Client should send the raw viewport bbox; the server pads once for the
    forcing cache and returns a zoom-tiered display window (rendered fully).

    ``terrain`` toggles WindNinja terrain-response downscaling (default on).
    """
    bbox, start, end = _parse_window(
        lat_min, lon_min, lat_max, lon_max, start_utc, end_utc
    )

    etag_src = (
        f"{model_id}|{lat_min:.4f},{lon_min:.4f},{lat_max:.4f},{lon_max:.4f}|"
        f"{start.isoformat()}|{end.isoformat()}|{zoom}|t={int(terrain)}"
    )
    etag = hashlib.sha1(etag_src.encode()).hexdigest()[:16]
    response.headers["ETag"] = f'"{etag}"'
    response.headers["Cache-Control"] = f"public, max-age={int(WIND_TTL_S)}"

    try:
        payload = get_display_series(
            model_id,
            bbox,
            start=start,
            end=end,
            zoom=zoom,
            enable_terrain=terrain,
        )
    except ForecastRateLimitError as exc:
        response.headers["Cache-Control"] = "no-store"
        return {
            "error": "rate_limited",
            "message": str(exc),
            "bbox": bbox.model_dump(),
        }
    if payload is None:
        response.headers["Cache-Control"] = "no-store"
        return {
            "error": "unavailable",
            "message": f"{model_id} wind unavailable",
            "bbox": bbox.model_dump(),
        }
    return payload


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


@router.get("/wind/area/stream")
def wind_area_stream(
    lat_min: float,
    lon_min: float,
    lat_max: float,
    lon_max: float,
    model_id: str = "ecmwf",
    start_utc: str | None = None,
    end_utc: str | None = None,
    zoom: float | None = None,
    terrain: bool = True,
) -> StreamingResponse:
    """SSE stream of wind-area stages, then a final result event with the series."""
    bbox, start, end = _parse_window(
        lat_min, lon_min, lat_max, lon_max, start_utc, end_utc
    )
    q: queue.Queue[Any] = queue.Queue()

    def on_progress(event: dict[str, Any]) -> None:
        q.put(event)

    def worker() -> None:
        try:
            payload = get_display_series(
                model_id,
                bbox,
                start=start,
                end=end,
                zoom=zoom,
                enable_terrain=terrain,
                on_progress=on_progress,
            )
            if payload is None:
                q.put(
                    {
                        "type": "error",
                        "message": f"{model_id} wind unavailable",
                    }
                )
            else:
                q.put({"type": "result", "series": payload})
        except ForecastRateLimitError as exc:
            q.put({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            q.put({"type": "error", "message": str(exc)})
        finally:
            q.put(_SENTINEL)

    thread = threading.Thread(target=worker, name="wind-area-stream", daemon=True)
    thread.start()

    def event_gen() -> Iterator[str]:
        while True:
            item = q.get()
            if item is _SENTINEL:
                break
            yield _sse(item)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
