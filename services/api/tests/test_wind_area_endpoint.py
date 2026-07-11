"""Tests for GET /wind/area endpoint (mocked field service)."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_wind_area_unavailable():
    with patch("app.routes.forecast.get_display_series", return_value=None):
        res = client.get(
            "/wind/area",
            params={
                "lat_min": 50.0,
                "lon_min": -2.0,
                "lat_max": 51.0,
                "lon_max": -1.0,
                "model_id": "ecmwf",
            },
        )
    assert res.status_code == 200
    data = res.json()
    assert data["error"] == "unavailable"


def test_wind_area_happy_path():
    payload = {
        "lat0": 50.0,
        "lon0": -2.0,
        "dlat": 0.1,
        "dlon": 0.1,
        "nlat": 2,
        "nlon": 2,
        "times": ["2026-07-11T00:00:00Z"],
        "u": [[1.0, 1.0, 1.0, 1.0]],
        "v": [[0.0, 0.0, 0.0, 0.0]],
        "speed_kn": [[1.9, 1.9, 1.9, 1.9]],
        "model_id": "ecmwf",
        "bbox": {"lat_min": 50.0, "lon_min": -2.0, "lat_max": 51.0, "lon_max": -1.0},
    }
    with patch("app.routes.forecast.get_display_series", return_value=payload):
        res = client.get(
            "/wind/area",
            params={
                "lat_min": 50.0,
                "lon_min": -2.0,
                "lat_max": 51.0,
                "lon_max": -1.0,
                "model_id": "ecmwf",
                "start_utc": "2026-07-11T00:00:00Z",
                "end_utc": "2026-07-11T12:00:00Z",
            },
        )
    assert res.status_code == 200
    data = res.json()
    assert data["model_id"] == "ecmwf"
    assert len(data["times"]) == 1
