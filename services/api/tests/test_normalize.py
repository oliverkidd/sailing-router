"""Tests for WindField.regrid + resample_hourly canonical normalization."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from app.metocean import WindField
from app.schemas import BBox
from app.util.bbox import CANON_RES_DEG, canonical_grid, hourly_epochs


def test_regrid_preserves_linear_field():
    # Coarse 1° grid: u = lon index, v = 1
    lats = np.array([50.0, 51.0, 52.0])
    lons = np.array([-2.0, -1.0, 0.0])
    u = np.zeros((1, 3, 3))
    v = np.ones((1, 3, 3))
    for j in range(3):
        u[0, :, j] = float(j)
    t0 = datetime(2026, 7, 11, 0, tzinfo=UTC).timestamp()
    field = WindField.build("ecmwf", [t0], lats, lons, u, v)

    region = BBox(lat_min=50.0, lon_min=-2.0, lat_max=52.0, lon_max=0.0)
    # Use a coarser-than-default target for a tiny test (still bilinear).
    lats_t = np.linspace(50.0, 52.0, 5)
    lons_t = np.linspace(-2.0, 0.0, 5)
    out = field.regrid(lats_t, lons_t)
    assert out.lats.shape == (5,)
    assert out.lons.shape == (5,)
    assert out.u10.shape == (1, 5, 5)
    # Left edge ~0, right edge ~2
    assert out.u10[0, 2, 0] < out.u10[0, 2, -1]


def test_resample_hourly_from_3h():
    lats = np.linspace(50.0, 51.0, 3)
    lons = np.linspace(-2.0, -1.0, 3)
    times = [
        datetime(2026, 7, 11, 0, tzinfo=UTC).timestamp(),
        datetime(2026, 7, 11, 3, tzinfo=UTC).timestamp(),
    ]
    u = np.zeros((2, 3, 3))
    v = np.ones((2, 3, 3))
    u[0] = 0.0
    u[1] = 3.0
    field = WindField.build("ecmwf", times, lats, lons, u, v)
    out = field.resample_hourly(times[0], times[1])
    assert out.n_times == 4  # 00, 01, 02, 03
    # Exact 1-hour spacing
    diffs = np.diff(out.times)
    assert np.allclose(diffs, 3600.0)
    # Linear: at +1h, u ≈ 1
    assert abs(float(out.u10[1, 1, 1]) - 1.0) < 1e-6
    assert abs(float(out.u10[2, 1, 1]) - 2.0) < 1e-6


def test_canonical_grid_identical_for_same_region():
    region = BBox(lat_min=50.0, lon_min=-2.0, lat_max=51.0, lon_max=-1.0)
    a = canonical_grid(region)
    b = canonical_grid(region)
    assert np.allclose(a[0], b[0])
    assert np.allclose(a[1], b[1])
    # Spacing is a multiple of CANON_RES_DEG
    if a[0].size > 1:
        assert abs(float(a[0][1] - a[0][0]) % CANON_RES_DEG) < 1e-9 or abs(
            float(a[0][1] - a[0][0]) - CANON_RES_DEG
        ) < 1e-9


def test_hourly_epochs():
    start = datetime(2026, 7, 11, 0, 30, tzinfo=UTC)
    end = datetime(2026, 7, 11, 2, 10, tzinfo=UTC)
    epochs = hourly_epochs(start, end)
    assert len(epochs) >= 3
    assert all(e % 3600 == 0 for e in epochs)
