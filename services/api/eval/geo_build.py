"""Land and depth for eval cases.

Open water and the synthetic island never touch the chart. Chart cases use the
same ``ensure_geo_context`` path as ``/route``.
"""

from __future__ import annotations

import math

import numpy as np
from shapely.geometry import Polygon

from app.geo import DepthField, GeoContext, LandMask
from app.schemas import Course, LatLon


def open_geo(points: list[LatLon], min_depth_m: float, margin_deg: float = 1.5) -> GeoContext:
    """Deep water, no land. The grid is wide so a beating boat is not 'shoal' off the edge."""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lat0, lat1 = min(lats) - margin_deg, max(lats) + margin_deg
    lon0, lon1 = min(lons) - margin_deg, max(lons) + margin_deg
    depth = DepthField(
        lats=np.array([lat0, lat1], dtype=float),
        lons=np.array([lon0, lon1], dtype=float),
        depth_m=np.array([[100.0, 100.0], [100.0, 100.0]]),
    )
    return GeoContext.build(
        land=LandMask([]),
        depth=depth,
        exclusion_rings=[],
        min_depth_m=min_depth_m,
    )


def island_geo(
    points: list[LatLon],
    center: LatLon,
    *,
    east_nm: float,
    north_nm: float,
    min_depth_m: float,
) -> GeoContext:
    """Open water plus one rectangular island. Polygon is (lon, lat), matching LandMask."""
    geo = open_geo(points, min_depth_m)
    lat, lon = center
    dlat = (north_nm / 2.0) / 60.0
    cos_lat = max(0.2, math.cos(math.radians(lat)))
    dlon = (east_nm / 2.0) / (60.0 * cos_lat)
    ring = [
        (lon - dlon, lat - dlat),
        (lon + dlon, lat - dlat),
        (lon + dlon, lat + dlat),
        (lon - dlon, lat + dlat),
    ]
    geo.land = LandMask([Polygon(ring)])
    return geo


def gap_geo(min_depth_m: float) -> GeoContext:
    """Same shoal-and-gap as ``validate_routing._build_gap_geo``."""
    lat_min, lat_max = 50.74, 50.79
    lon_min, lon_max = -1.40, -1.28
    lats = np.linspace(lat_min, lat_max, 80)
    lons = np.linspace(lon_min, lon_max, 80)
    depth = np.full((lats.size, lons.size), 20.0, dtype=np.float64)
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            if -1.355 <= lo <= -1.325 and la >= 50.765:
                depth[i, j] = 1.0
    return GeoContext.build(
        land=LandMask([]),
        depth=DepthField(lats, lons, depth),
        exclusion_rings=[],
        min_depth_m=min_depth_m,
    )


def channel_geo(min_depth_m: float) -> GeoContext:
    """Same narrow corridor as ``validate_routing._build_channel_geo``."""
    lat_min, lat_max = 50.745, 50.785
    lon_min, lon_max = -1.370, -1.310
    lats = np.linspace(lat_min, lat_max, 100)
    lons = np.linspace(lon_min, lon_max, 80)
    depth = np.full((lats.size, lons.size), 1.0, dtype=np.float64)
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            if abs(lo - (-1.340)) <= 0.004:
                depth[i, j] = 20.0
            if la <= 50.754 and -1.360 <= lo <= -1.320:
                depth[i, j] = 20.0
    return GeoContext.build(
        land=LandMask([]),
        depth=DepthField(lats, lons, depth),
        exclusion_rings=[],
        min_depth_m=min_depth_m,
    )


def chart_geo(course: Course) -> GeoContext:
    from app.geo.fetch import ensure_geo_context

    # adaptive pad via course_bbox / effective_course_margin_deg
    return ensure_geo_context(course)
