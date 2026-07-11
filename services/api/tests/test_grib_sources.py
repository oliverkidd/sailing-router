"""Tests for GRIB helpers + WindField assembly (mocked downloads, no live network)."""

from __future__ import annotations

import numpy as np

from app.forecast.grib_source import assemble_windfield, parse_idx, find_idx_range
from app.metocean import WindField
from app.schemas import BBox


def test_parse_idx_and_range():
    idx = (
        "1:0:d=2026071100:UGRD:10 m above ground:3 hour fcst:\n"
        "2:1500:d=2026071100:VGRD:10 m above ground:3 hour fcst:\n"
        "3:3000:d=2026071100:TMP:2 m above ground:3 hour fcst:\n"
    )
    records = parse_idx(idx)
    assert len(records) == 3
    u = find_idx_range(records, ":UGRD:10 m above ground:")
    v = find_idx_range(records, ":VGRD:10 m above ground:")
    assert u == (0, 1499)
    assert v == (1500, 2999)


def test_assemble_windfield_and_subset():
    lats = np.linspace(50.0, 52.0, 5)
    lons = np.linspace(-2.0, 1.0, 7)
    per_step = []
    for i in range(3):
        u = np.full((5, 7), float(i), dtype=float)
        v = np.ones((5, 7), dtype=float)
        per_step.append((1_000_000.0 + i * 3600, u, v))
    field = assemble_windfield("ecmwf", per_step, lats, lons)
    field.validate()
    assert field.n_times == 3
    assert field.u10[1, 0, 0] == 1.0

    sub = field.subset_bbox(BBox(lat_min=50.5, lon_min=-1.0, lat_max=51.5, lon_max=0.5))
    assert sub.lats[0] <= 50.5
    assert sub.lats[-1] >= 51.5


def test_windfield_build_flips_descending_lats():
    lats = np.array([52.0, 51.0, 50.0])
    lons = np.array([-2.0, -1.0, 0.0])
    u = np.arange(9, dtype=float).reshape(1, 3, 3)
    v = np.zeros((1, 3, 3))
    field = WindField.build("gfs", [0.0], lats, lons, u, v)
    assert field.lats[0] < field.lats[-1]
    # After flip, first lat row should be the old last row.
    assert field.u10[0, 0, 0] == 6.0


def test_concat_time_dedupes():
    lats = np.array([50.0, 51.0])
    lons = np.array([-1.0, 0.0])
    f1 = WindField.build("ecmwf", [0.0], lats, lons, np.zeros((1, 2, 2)), np.ones((1, 2, 2)))
    f2 = WindField.build("ecmwf", [3600.0], lats, lons, np.ones((1, 2, 2)), np.zeros((1, 2, 2)))
    f3 = WindField.build("ecmwf", [0.0], lats, lons, np.full((1, 2, 2), 9.0), np.zeros((1, 2, 2)))
    cat = WindField.concat_time([f1, f2, f3])
    assert cat.n_times == 2
    assert cat.u10[0, 0, 0] == 0.0  # first occurrence of t=0 kept
