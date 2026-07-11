"""Tests for display-series interpolation + budget dims."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from app.overlays.grid import display_series_payload
from app.metocean import WindField
from app.schemas import BBox
from app.util.bbox import TARGET_RES_DEG, budget_dims, region_bbox


def test_budget_dims_respects_cap():
    bbox = BBox(lat_min=40.0, lon_min=-10.0, lat_max=50.0, lon_max=10.0)
    nlat, nlon = budget_dims(bbox, TARGET_RES_DEG, max_pts=96 * 96)
    assert nlat * nlon <= 96 * 96
    assert nlat >= 2 and nlon >= 2


def test_region_bbox_pads_and_snaps():
    bbox = BBox(lat_min=50.1, lon_min=-1.9, lat_max=50.9, lon_max=-1.1)
    r = region_bbox(bbox, pad_deg=1.0, snap_deg=0.25)
    assert r.lat_min <= 49.1
    assert r.lat_max >= 51.9


def test_display_series_linear_field():
    lats = np.linspace(50.0, 51.0, 5)
    lons = np.linspace(-2.0, -1.0, 5)
    # u increases with lon index; v constant
    u = np.zeros((2, 5, 5))
    v = np.ones((2, 5, 5))
    for j in range(5):
        u[:, :, j] = float(j)
    times = [
        datetime(2026, 7, 11, 0, tzinfo=UTC).timestamp(),
        datetime(2026, 7, 11, 3, tzinfo=UTC).timestamp(),
    ]
    field = WindField.build("ecmwf", times, lats, lons, u, v)
    bbox = BBox(lat_min=50.0, lon_min=-2.0, lat_max=51.0, lon_max=-1.0)
    nlat, nlon = 4, 4
    payload = display_series_payload(
        field,
        bbox,
        nlat,
        nlon,
        start=datetime(2026, 7, 11, 0, tzinfo=UTC),
        end=datetime(2026, 7, 11, 3, tzinfo=UTC),
    )
    assert payload["nlat"] == 4
    assert len(payload["times"]) == 2
    assert len(payload["u"]) == 2
    assert len(payload["u"][0]) == 16
    # Left edge ~0, right edge ~4
    assert payload["u"][0][0] < payload["u"][0][-1]
