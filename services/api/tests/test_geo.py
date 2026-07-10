from __future__ import annotations

import numpy as np
import pytest

from app.config import DATA_DIR
from app.geo import (
    DepthField,
    GeoContext,
    LandMask,
    bearing,
    destination,
    distance_nm,
)

LAND = DATA_DIR / "masks" / "land_aegean.geojson"


def test_distance_and_bearing_sane():
    a = (36.0, 24.0)
    b = (37.0, 24.0)  # 1 degree of latitude north
    assert distance_nm(a, b) == pytest.approx(60.0, abs=0.6)  # ~60 nm / deg lat
    assert bearing(a, b) == pytest.approx(0.0, abs=0.5)  # due north
    east = (36.0, 25.0)
    assert bearing(a, east) == pytest.approx(90.0, abs=1.0)


def test_destination_roundtrip():
    origin = (36.5, 24.5)
    dest = destination(origin, 45.0, 10.0)
    assert distance_nm(origin, dest) == pytest.approx(10.0, abs=0.05)
    assert bearing(origin, dest) == pytest.approx(45.0, abs=0.5)


def test_island_crossing_rejected():
    land = LandMask.from_geojson(LAND)
    assert not land.is_empty
    # Center of the sample island is on land.
    assert land.on_land((36.72, 24.45))
    # A leg from west to east straight through the island crosses land.
    assert land.segment_crosses_land((36.72, 24.0), (36.72, 25.0))
    # A leg well south of the island does not.
    assert not land.segment_crosses_land((36.2, 24.0), (36.2, 25.0))


def test_depth_flags_shallow_point():
    lats = np.array([36.0, 37.0])
    lons = np.array([24.0, 25.0])
    # 2 m everywhere.
    depth = DepthField(lats, lons, np.full((2, 2), 2.0))
    assert depth.too_shallow((36.5, 24.5), min_depth_m=3.0)
    assert not depth.too_shallow((36.5, 24.5), min_depth_m=1.0)


def test_exclusion_membership():
    ring = [(36.4, 24.0), (36.4, 24.2), (36.6, 24.2), (36.6, 24.0)]
    ctx = GeoContext.build(land=None, depth=None, exclusion_rings=[ring], min_depth_m=3.0)
    assert ctx.in_exclusion((36.5, 24.1))
    assert not ctx.in_exclusion((36.5, 24.5))
