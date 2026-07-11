"""Tests for SQLite catalog covering + region lookups."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from app import catalog
from app.metocean import WindField
from app.schemas import BBox


def _field(t0: float | None = None, hours: int = 48) -> WindField:
    base = t0 if t0 is not None else time.time()
    return WindField.build(
        "ecmwf",
        [base, base + 3600 * hours],
        np.linspace(49.0, 53.0, 5),
        np.linspace(-3.0, 2.0, 5),
        np.zeros((2, 5, 5)),
        np.ones((2, 5, 5)),
    )


def test_catalog_covering_and_freshness(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    monkeypatch.setattr(catalog, "_DB_PATH", db)
    catalog.init_db()

    field = _field()
    region = BBox(lat_min=49.0, lon_min=-3.0, lat_max=53.0, lon_max=2.0)
    zpath = tmp_path / "f.zarr"
    zpath.mkdir()
    catalog.upsert("ecmwf", datetime.now(UTC), region, field, zpath, ttl_s=3600)

    hit = catalog.find_covering(
        "ecmwf",
        BBox(lat_min=50.0, lon_min=-2.0, lat_max=51.0, lon_max=-1.0),
        start_epoch=field.times[0],
        end_epoch=field.times[0] + 3600,
    )
    assert hit is not None
    assert Path(hit.zarr_path) == zpath

    miss = catalog.find_covering(
        "ecmwf",
        BBox(lat_min=10.0, lon_min=10.0, lat_max=11.0, lon_max=11.0),
        start_epoch=field.times[0],
        end_epoch=field.times[0] + 3600,
    )
    assert miss is None

    # Expired
    catalog.upsert("ecmwf", datetime.now(UTC), region, field, zpath, ttl_s=-1)
    expired = catalog.find_covering(
        "ecmwf",
        BBox(lat_min=50.0, lon_min=-2.0, lat_max=51.0, lon_max=-1.0),
        start_epoch=field.times[0],
        end_epoch=field.times[0] + 3600,
    )
    assert expired is None


def test_find_region_field_and_extend_updates_t_end(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    monkeypatch.setattr(catalog, "_DB_PATH", db)
    catalog.init_db()

    t0 = datetime(2026, 7, 11, 0, tzinfo=UTC).timestamp()
    field = _field(t0=t0, hours=12)
    region = BBox(lat_min=49.0, lon_min=-3.0, lat_max=53.0, lon_max=2.0)
    cycle = datetime(2026, 7, 11, 0, tzinfo=UTC)
    zpath = tmp_path / "f.zarr"
    zpath.mkdir()
    catalog.upsert("ecmwf", cycle, region, field, zpath, ttl_s=3600)

    row = catalog.find_region_field("ecmwf", cycle.isoformat(), region)
    assert row is not None
    assert abs(row.t_end - float(field.times[-1])) < 1.0

    # Extend: upsert same path with longer field → t_end updates.
    extended = _field(t0=t0, hours=36)
    catalog.upsert("ecmwf", cycle, region, extended, zpath, ttl_s=3600)
    row2 = catalog.find_region_field("ecmwf", cycle.isoformat(), region)
    assert row2 is not None
    assert row2.t_end > row.t_end
    assert abs(row2.t_end - float(extended.times[-1])) < 1.0
