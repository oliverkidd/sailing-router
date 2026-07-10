from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from app.config import FIXTURES_DIR
from app.forecast.openmeteo import OpenMeteoProvider
from app.metocean import WindField
from app.schemas import BBox

AEGEAN_BBOX = BBox(lat_min=36.0, lon_min=24.0, lat_max=37.0, lon_max=25.0)


@pytest.fixture()
def fixture_json() -> list:
    p = Path(FIXTURES_DIR) / "openmeteo_aegean.json"
    return json.loads(p.read_text())


def test_openmeteo_builds_windfield_from_fixture(fixture_json):
    # Golden/offline: parse a checked-in European response, no network.
    lats = np.array([36.0, 37.0])
    lons = np.array([24.0, 25.0])
    wf = OpenMeteoProvider._to_wind_field("ecmwf", fixture_json, lats, lons)
    assert wf.u10.shape == (3, 2, 2)
    assert wf.n_times == 3


def test_wind_convention_from_direction(fixture_json):
    lats = np.array([36.0, 37.0])
    lons = np.array([24.0, 25.0])
    wf = OpenMeteoProvider._to_wind_field("ecmwf", fixture_json, lats, lons)
    t0 = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
    tws, twd = wf.wind_at(36.0, 24.0, t0)
    # 8 m/s -> ~15.55 kn; TWD should read back ~315 (NW, blows from).
    assert tws == pytest.approx(8.0 * 1.943844, abs=0.1)
    assert twd == pytest.approx(315.0, abs=0.5)


def test_forecast_exhausted_outside_horizon(fixture_json):
    lats = np.array([36.0, 37.0])
    lons = np.array([24.0, 25.0])
    wf = OpenMeteoProvider._to_wind_field("ecmwf", fixture_json, lats, lons)
    too_late = datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    assert wf.wind_at(36.0, 24.0, too_late) is None


def test_windfield_npz_roundtrip(tmp_path, fixture_json):
    lats = np.array([36.0, 37.0])
    lons = np.array([24.0, 25.0])
    wf = OpenMeteoProvider._to_wind_field("ecmwf", fixture_json, lats, lons)
    p = tmp_path / "wf.npz"
    wf.save_npz(p)
    wf2 = WindField.load_npz(p)
    t0 = datetime(2026, 7, 10, 1, 0, tzinfo=UTC)
    assert wf.wind_at(36.5, 24.5, t0) == pytest.approx(wf2.wind_at(36.5, 24.5, t0))


def test_provider_fetch_with_mocked_http(fixture_json):
    respx = pytest.importorskip("respx")
    import httpx

    with respx.mock:
        respx.get("https://api.open-meteo.com/v1/forecast").mock(
            return_value=httpx.Response(200, json=fixture_json)
        )
        provider = OpenMeteoProvider(grid_n=2)
        wf = provider.fetch("ecmwf", AEGEAN_BBOX, cycle=None, horizon_h=3)
    assert wf.n_times == 3
    assert wf.u10.shape == (3, 2, 2)
