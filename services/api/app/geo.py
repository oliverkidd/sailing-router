"""Geodesy + land / depth / exclusion masks.

Geodesy uses great-circle (haversine) math; for the short legs the router
integrates (minutes of sailing) the difference from rhumb lines is negligible.
TODO(v2): rhumb-line option for very long legs.

Masks are pluggable:
- LandMask: coastline polygons (OSM / Natural Earth) -> on_land / segment_crosses_land.
- DepthField: EMODnet bathymetry grid -> depth / too_shallow.
- Exclusions: user-drawn polygons -> in_exclusion.

All are assembled into a GeoContext the router queries.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, shape
from shapely.prepared import prep

from .config import DATA_DIR
from .schemas import Course, LatLon

EARTH_R_NM = 3440.065  # nautical miles

_LAND_GEOJSON = DATA_DIR / "masks" / "land_aegean.geojson"
_DEPTH_NPZ = DATA_DIR / "masks" / "depth_grid.npz"


# --- geodesy ----------------------------------------------------------------


def distance_nm(a: LatLon, b: LatLon) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_R_NM * math.asin(min(1.0, math.sqrt(h)))


def bearing(a: LatLon, b: LatLon) -> float:
    """Initial great-circle bearing from a to b, compass degrees."""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360.0


def destination(origin: LatLon, brng: float, dist_nm: float) -> LatLon:
    """Great-circle destination from origin along bearing for dist_nm."""
    lat1, lon1 = math.radians(origin[0]), math.radians(origin[1])
    br = math.radians(brng)
    ad = dist_nm / EARTH_R_NM
    lat2 = math.asin(math.sin(lat1) * math.cos(ad) + math.cos(lat1) * math.sin(ad) * math.cos(br))
    lon2 = lon1 + math.atan2(
        math.sin(br) * math.sin(ad) * math.cos(lat1),
        math.cos(ad) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), (math.degrees(lon2) + 540.0) % 360.0 - 180.0


# --- land mask --------------------------------------------------------------


class LandMask:
    """Coastline polygons; shapely uses (x=lon, y=lat)."""

    def __init__(self, polygons: list[Polygon] | None = None):
        polys = polygons or []
        self._mp = MultiPolygon([p for p in polys]) if polys else MultiPolygon()
        self._prepared = prep(self._mp) if not self._mp.is_empty else None

    @classmethod
    def from_geojson(cls, path: str | Path) -> LandMask:
        p = Path(path)
        if not p.exists():
            return cls([])
        gj = json.loads(p.read_text())
        polys: list[Polygon] = []
        feats = gj.get("features", []) if gj.get("type") == "FeatureCollection" else [gj]
        for f in feats:
            geom = shape(f["geometry"] if "geometry" in f else f)
            if isinstance(geom, Polygon):
                polys.append(geom)
            elif isinstance(geom, MultiPolygon):
                polys.extend(list(geom.geoms))
        return cls(polys)

    @property
    def is_empty(self) -> bool:
        return self._prepared is None

    def on_land(self, pos: LatLon) -> bool:
        if self._prepared is None:
            return False
        return self._prepared.covers(Point(pos[1], pos[0]))

    def segment_crosses_land(self, a: LatLon, b: LatLon) -> bool:
        if self._prepared is None:
            return False
        seg = LineString([(a[1], a[0]), (b[1], b[0])])
        return self._prepared.intersects(seg)


# --- depth field ------------------------------------------------------------


@dataclass
class DepthField:
    """Regular grid of water depth (metres, positive down). Missing => deep water."""

    lats: np.ndarray
    lons: np.ndarray
    depth_m: np.ndarray  # (nlat, nlon)

    def depth(self, pos: LatLon) -> float:
        lat, lon = pos
        if lat < self.lats[0] or lat > self.lats[-1] or lon < self.lons[0] or lon > self.lons[-1]:
            return 9999.0  # outside grid: assume deep, safe water
        i = int(np.clip(np.searchsorted(self.lats, lat) - 1, 0, self.lats.size - 2))
        j = int(np.clip(np.searchsorted(self.lons, lon) - 1, 0, self.lons.size - 2))
        la0, la1 = self.lats[i], self.lats[i + 1]
        lo0, lo1 = self.lons[j], self.lons[j + 1]
        fy = 0.0 if la1 == la0 else (lat - la0) / (la1 - la0)
        fx = 0.0 if lo1 == lo0 else (lon - lo0) / (lo1 - lo0)
        d0 = self.depth_m[i, j] * (1 - fx) + self.depth_m[i, j + 1] * fx
        d1 = self.depth_m[i + 1, j] * (1 - fx) + self.depth_m[i + 1, j + 1] * fx
        return float(d0 * (1 - fy) + d1 * fy)

    def too_shallow(self, pos: LatLon, min_depth_m: float) -> bool:
        return self.depth(pos) < min_depth_m


# --- geo context ------------------------------------------------------------


@dataclass
class GeoContext:
    land: LandMask = field(default_factory=lambda: LandMask([]))
    depth: DepthField | None = None
    exclusions: list[Polygon] = field(default_factory=list)
    min_depth_m: float = 3.0

    @classmethod
    def build(
        cls,
        land: LandMask | None,
        depth: DepthField | None,
        exclusion_rings: list[list[LatLon]],
        min_depth_m: float,
    ) -> GeoContext:
        polys = [Polygon([(lon, lat) for lat, lon in ring]) for ring in exclusion_rings if len(ring) >= 3]
        return cls(
            land=land or LandMask([]),
            depth=depth,
            exclusions=polys,
            min_depth_m=min_depth_m,
        )

    def in_exclusion(self, pos: LatLon) -> bool:
        pt = Point(pos[1], pos[0])
        return any(poly.covers(pt) for poly in self.exclusions)

    def too_shallow(self, pos: LatLon) -> bool:
        if self.depth is None:
            return False
        return self.depth.too_shallow(pos, self.min_depth_m)

    def blocked_point(self, pos: LatLon) -> bool:
        return self.land.on_land(pos) or self.too_shallow(pos) or self.in_exclusion(pos)

    def blocked_segment(self, a: LatLon, b: LatLon) -> bool:
        return self.land.segment_crosses_land(a, b)


def load_default_land() -> LandMask:
    """Load the prebuilt coastline mask (Natural Earth clip via make fetch-fixtures;
    ships with a small Aegean sample so the demo works offline)."""
    return LandMask.from_geojson(_LAND_GEOJSON)


def load_default_depth() -> DepthField | None:
    """Load a prebuilt EMODnet depth grid if present (optional)."""
    if not _DEPTH_NPZ.exists():
        return None
    z = np.load(_DEPTH_NPZ)
    return DepthField(z["lats"], z["lons"], z["depth_m"])


def course_bbox(course: Course, margin_deg: float = 1.0):
    """Bounding box covering the whole course (+margin) for metocean fetches."""
    from .schemas import BBox

    pts = [course.start, course.finish, *[m.position for m in course.marks]]
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    return BBox(
        lat_min=min(lats) - margin_deg,
        lon_min=min(lons) - margin_deg,
        lat_max=max(lats) + margin_deg,
        lon_max=max(lons) + margin_deg,
    )


def context_for_course(course: Course) -> GeoContext:
    """Assemble the GeoContext the router queries for a given course."""
    return GeoContext.build(
        land=load_default_land(),
        depth=load_default_depth(),
        exclusion_rings=[[tuple(p) for p in ring] for ring in course.exclusions],
        min_depth_m=course.min_depth_m,
    )
