"""Tests for depth isobaths, raster orientation, and chart coastline requirements."""

from __future__ import annotations

import numpy as np
from shapely.geometry import box

from app.geo import DepthField
from app.geo.contours import depth_contours
from app.geo.sources import _grid_from_raster
from app.schemas import BBox


def _bowl_depth(n: int = 40) -> DepthField:
    """Synthetic bowl: deep centre, shallow edges (metres positive-down)."""
    lats = np.linspace(50.0, 51.0, n)
    lons = np.linspace(-1.0, 0.0, n)
    yy, xx = np.meshgrid(np.linspace(-1, 1, n), np.linspace(-1, 1, n), indexing="ij")
    depth = 80.0 * (1.0 - np.sqrt(xx**2 + yy**2).clip(0, 1))
    return DepthField(lats=lats, lons=lons, depth_m=depth)


def test_grid_from_raster_flips_north_up():
    """Row 0 of a north-up GeoTIFF is north; stored grid must be south-first."""
    arr = np.array([[10.0, 11.0], [20.0, 21.0], [30.0, 31.0]])
    bounds = (-1.0, 50.0, 0.0, 51.0)  # west, south, east, north
    bbox = BBox(lat_min=50.0, lon_min=-1.0, lat_max=51.0, lon_max=0.0)
    lats, lons, out = _grid_from_raster(arr, bounds, bbox)
    assert lats[0] < lats[-1]
    assert abs(lats[0] - 50.0) < 1e-9
    assert abs(lats[-1] - 51.0) < 1e-9
    assert out[0, 0] == 30.0
    assert out[-1, 0] == 10.0
    assert abs(lons[0] - -1.0) < 1e-9


def test_depth_contours_emit_expected_levels():
    field = _bowl_depth(n=50)
    fc = depth_contours(field, levels=[5.0, 10.0, 50.0], max_depth_m=50.0)
    assert fc["type"] == "FeatureCollection"
    depths = {f["properties"]["depth_m"] for f in fc["features"]}
    assert depths <= {5.0, 10.0, 50.0}
    assert 5.0 in depths or 10.0 in depths
    for f in fc["features"]:
        assert f["geometry"]["type"] == "LineString"


def test_depth_contours_clipped_away_from_land():
    field = _bowl_depth(n=40)
    land = [box(-1.0, 50.0, 0.0, 51.0)]
    fc = depth_contours(field, levels=[5.0, 10.0], land_polys=land)
    assert fc["features"] == []


def test_gshhg_land_solent_includes_isle_of_wight():
    from app.config import GSHHG_LAND_SHP
    from app.geo.sources import gshhg_land
    from shapely.geometry import Point

    if not GSHHG_LAND_SHP.exists():
        import pytest

        pytest.skip("GSHHG not installed")

    bbox = BBox(lat_min=50.55, lon_min=-1.6, lat_max=50.85, lon_max=-1.05)
    polys = gshhg_land(bbox, allow_fallback=False)
    assert len(polys) >= 1
    iow = Point(-1.3, 50.68)
    assert any(p.covers(iow) or p.contains(iow) for p in polys)
