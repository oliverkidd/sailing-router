"""Tests for field_service cache + delta-fetch + coalescing (mocked GRIB source)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import numpy as np

from app import catalog, field_service
from app.forecast import grib_source
from app.forecast.grib_source import GribSource, SourceSpec
from app.metocean import WindField
from app.schemas import BBox


class CountingSource(GribSource):
    def __init__(self):
        self.spec = SourceSpec(
            model_id="ecmwf",
            connector="ecmwf-ifs",
            resolution_deg=0.25,
            cycle_hours=(0, 6, 12, 18),
            step_hours=tuple(range(0, 73, 3)),
            publish_delay_h=0.0,
        )
        self.calls = 0
        self.last_steps: list[int] = []
        self._lock = threading.Lock()

    def latest_cycle(self, now=None):
        return datetime(2026, 7, 11, 0, tzinfo=UTC)

    def fetch(self, bbox, cycle, steps):
        with self._lock:
            self.calls += 1
            self.last_steps = list(steps)
        time.sleep(0.05)  # force overlap for coalescing test
        nlat, nlon = 4, 4
        # Native 3-hourly frames covering the requested steps.
        times = [cycle.timestamp() + s * 3600 for s in steps]
        return WindField.build(
            "ecmwf",
            times,
            np.linspace(bbox.lat_min, bbox.lat_max, nlat),
            np.linspace(bbox.lon_min, bbox.lon_max, nlon),
            np.zeros((len(times), nlat, nlon)),
            np.ones((len(times), nlat, nlon)),
        )


def test_get_area_field_cache_and_coalesce(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "_DB_PATH", tmp_path / "catalog.db")
    monkeypatch.setattr(field_service.store_wind, "FORECAST_DIR", tmp_path / "forecast")
    (tmp_path / "forecast").mkdir()
    catalog.init_db()

    src = CountingSource()
    monkeypatch.setitem(grib_source.REGISTRY, "ecmwf", src)
    monkeypatch.setattr(field_service, "get_source", lambda mid: src)
    field_service._lru.clear()
    field_service._locks.clear()

    bbox = BBox(lat_min=50.0, lon_min=-2.0, lat_max=51.0, lon_max=-1.0)
    start = datetime(2026, 7, 11, 0, tzinfo=UTC)
    end = start + timedelta(hours=24)

    results: list[WindField | None] = [None, None]

    def worker(i: int):
        results[i] = field_service.get_area_field("ecmwf", bbox, start=start, end=end)

    t0 = threading.Thread(target=worker, args=(0,))
    t1 = threading.Thread(target=worker, args=(1,))
    t0.start()
    t1.start()
    t0.join()
    t1.join()

    assert results[0] is not None and results[1] is not None
    assert src.calls == 1  # coalesced
    # Canonical: hourly spacing
    diffs = np.diff(results[0].times)
    assert np.allclose(diffs, 3600.0)

    # Second call hits catalog/LRU — no extra fetch.
    again = field_service.get_area_field("ecmwf", bbox, start=start, end=end)
    assert again is not None
    assert src.calls == 1
    assert list((tmp_path / "forecast").glob("*.zarr"))


def test_delta_fetch_only_missing_hours(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "_DB_PATH", tmp_path / "catalog.db")
    monkeypatch.setattr(field_service.store_wind, "FORECAST_DIR", tmp_path / "forecast")
    (tmp_path / "forecast").mkdir()
    catalog.init_db()

    src = CountingSource()
    monkeypatch.setitem(grib_source.REGISTRY, "ecmwf", src)
    monkeypatch.setattr(field_service, "get_source", lambda mid: src)
    field_service._lru.clear()
    field_service._locks.clear()

    bbox = BBox(lat_min=50.0, lon_min=-2.0, lat_max=51.0, lon_max=-1.0)
    start = datetime(2026, 7, 11, 0, tzinfo=UTC)
    end24 = start + timedelta(hours=24)
    end48 = start + timedelta(hours=48)

    first = field_service.get_area_field("ecmwf", bbox, start=start, end=end24)
    assert first is not None
    assert src.calls == 1
    first_steps = list(src.last_steps)

    # Extend window — should fetch only steps bracketing the missing hours.
    second = field_service.get_area_field("ecmwf", bbox, start=start, end=end48)
    assert second is not None
    assert src.calls == 2
    # Second fetch should not re-request the entire first window's steps.
    assert max(src.last_steps) > max(first_steps)
    assert second.n_times > first.n_times
    # Still hourly after concat.
    assert np.allclose(np.diff(second.times), 3600.0)
