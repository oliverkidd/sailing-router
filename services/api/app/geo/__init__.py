"""Geodesy + land / depth / exclusion masks."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, shape
from shapely.prepared import prep

from ..schemas import BBox, Course, LatLon

EARTH_R_NM = 3440.065  # nautical miles


def distance_nm(a: LatLon, b: LatLon) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_R_NM * math.asin(min(1.0, math.sqrt(h)))


def bearing(a: LatLon, b: LatLon) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360.0


def destination(origin: LatLon, brng: float, dist_nm: float) -> LatLon:
    lat1, lon1 = math.radians(origin[0]), math.radians(origin[1])
    br = math.radians(brng)
    ad = dist_nm / EARTH_R_NM
    lat2 = math.asin(math.sin(lat1) * math.cos(ad) + math.cos(lat1) * math.sin(ad) * math.cos(br))
    lon2 = lon1 + math.atan2(
        math.sin(br) * math.sin(ad) * math.cos(lat1),
        math.cos(ad) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), (math.degrees(lon2) + 540.0) % 360.0 - 180.0


class LandMask:
    def __init__(self, polygons: list[Polygon] | None = None):
        polys = polygons or []
        self._mp = MultiPolygon([p for p in polys]) if polys else MultiPolygon()
        self._prepared = prep(self._mp) if not self._mp.is_empty else None
        # ~110 m cells — enough to collapse repeated segment samples.
        self._cell_cache: dict[tuple[int, int], bool] = {}

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

    @property
    def polygons(self) -> list[Polygon]:
        return [] if self._mp.is_empty else list(self._mp.geoms)

    @property
    def geometry(self) -> MultiPolygon:
        """Raw union geometry — for vectorised point-in-polygon rasterising."""
        return self._mp

    def islands(self) -> list[tuple[float, float, float]]:
        out: list[tuple[float, float, float]] = []
        for poly in self.polygons:
            c = poly.centroid
            minx, miny, maxx, maxy = poly.bounds
            r_nm = 0.5 * distance_nm((miny, minx), (maxy, maxx))
            out.append((c.y, c.x, r_nm))
        return out

    def on_land(self, pos: LatLon) -> bool:
        if self._prepared is None:
            return False
        key = (int(pos[0] * 1000), int(pos[1] * 1000))
        hit = self._cell_cache.get(key)
        if hit is not None:
            return hit
        out = self._prepared.covers(Point(pos[1], pos[0]))
        self._cell_cache[key] = out
        return out

    def segment_crosses_land(self, a: LatLon, b: LatLon) -> bool:
        if self._prepared is None:
            return False
        seg = LineString([(a[1], a[0]), (b[1], b[0])])
        return self._prepared.intersects(seg)


@dataclass
class DepthField:
    lats: np.ndarray
    lons: np.ndarray
    depth_m: np.ndarray
    _cell_cache: dict[tuple[int, int], float] = field(
        default_factory=dict, repr=False, compare=False
    )

    def depth(self, pos: LatLon) -> float:
        """Conservative depth (m, positive-down) at pos.

        Uses the minimum of the surrounding 2×2 cells so a narrow shoal between
        deep samples is not averaged away. Non-finite / missing → 0 (blocks).
        """
        lat, lon = pos
        if (
            lat < self.lats[0]
            or lat > self.lats[-1]
            or lon < self.lons[0]
            or lon > self.lons[-1]
        ):
            return 0.0
        i = int(np.clip(np.searchsorted(self.lats, lat) - 1, 0, self.lats.size - 2))
        j = int(np.clip(np.searchsorted(self.lons, lon) - 1, 0, self.lons.size - 2))
        key = (i, j)
        hit = self._cell_cache.get(key)
        if hit is not None:
            return hit
        cells = (
            self.depth_m[i, j],
            self.depth_m[i, j + 1],
            self.depth_m[i + 1, j],
            self.depth_m[i + 1, j + 1],
        )
        vals = [0.0 if not np.isfinite(v) or v < 0 else float(v) for v in cells]
        out = min(vals)
        self._cell_cache[key] = out
        return out

    def too_shallow(self, pos: LatLon, min_depth_m: float) -> bool:
        return self.depth(pos) < min_depth_m


@dataclass
class GeoContext:
    land: LandMask = field(default_factory=lambda: LandMask([]))
    depth: DepthField | None = None
    exclusions: list[Polygon] = field(default_factory=list)
    min_depth_m: float = 3.0
    depth_safety_margin_m: float = 1.0
    # ~90 m — enough for chart obstacles; 0.01 nm (~19 m) was ~5× more samples.
    segment_sample_nm: float = 0.05

    @classmethod
    def build(
        cls,
        land: LandMask | None,
        depth: DepthField | None,
        exclusion_rings: list[list[LatLon]],
        min_depth_m: float,
        depth_safety_margin_m: float = 1.0,
    ) -> GeoContext:
        polys = [Polygon([(lon, lat) for lat, lon in ring]) for ring in exclusion_rings if len(ring) >= 3]
        return cls(
            land=land or LandMask([]),
            depth=depth,
            exclusions=polys,
            min_depth_m=min_depth_m,
            depth_safety_margin_m=depth_safety_margin_m,
        )

    def in_exclusion(self, pos: LatLon) -> bool:
        pt = Point(pos[1], pos[0])
        return any(poly.covers(pt) for poly in self.exclusions)

    def too_shallow(self, pos: LatLon) -> bool:
        if self.depth is None:
            return False
        return self.depth.too_shallow(pos, self.min_depth_m + self.depth_safety_margin_m)

    def blocked_point(self, pos: LatLon) -> bool:
        return self.land.on_land(pos) or self.too_shallow(pos) or self.in_exclusion(pos)

    def blocked_segment(self, a: LatLon, b: LatLon) -> bool:
        if self.land.segment_crosses_land(a, b):
            return True
        dist = distance_nm(a, b)
        steps = max(2, int(dist / self.segment_sample_nm))
        for i in range(steps + 1):
            f = i / steps
            lat = a[0] + f * (b[0] - a[0])
            lon = a[1] + f * (b[1] - a[1])
            if self.blocked_point((lat, lon)):
                return True
        return False


# Shared wind/geo margin for prepare + /route (corridor around the course).
# Compact coastal races (Middle Sea, Aegean) need ~1° of sea room past the
# outermost mark so the waypoint chain is not pinned to a cape. Ocean-spanning
# courses (Fastnet) already have sea room — see ``effective_course_margin_deg``.
COURSE_WIND_MARGIN_DEG = 1.0
# Lon/lat span above this → clamp margin to 0.5° (Fastnet-scale boxes).
_LARGE_COURSE_SPAN_DEG = 6.0
_LARGE_COURSE_MARGIN_DEG = 0.5


def effective_course_margin_deg(
    course: Course, margin_deg: float = COURSE_WIND_MARGIN_DEG
) -> float:
    """Pad for geo/wind bbox: full margin on compact courses, clamped on huge ones."""
    pts = [course.start, course.finish, *[m.position for m in course.marks]]
    span = max(
        max(p[0] for p in pts) - min(p[0] for p in pts),
        max(p[1] for p in pts) - min(p[1] for p in pts),
    )
    if span >= _LARGE_COURSE_SPAN_DEG:
        return min(margin_deg, _LARGE_COURSE_MARGIN_DEG)
    return margin_deg


def course_bbox(course: Course, margin_deg: float = COURSE_WIND_MARGIN_DEG) -> BBox:
    """Course corridor bbox. Default margin matches COURSE_WIND_MARGIN_DEG (routing + prepare)."""
    pts = [course.start, course.finish, *[m.position for m in course.marks]]
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    pad = effective_course_margin_deg(course, margin_deg)
    return BBox(
        lat_min=min(lats) - pad,
        lon_min=min(lons) - pad,
        lat_max=max(lats) + pad,
        lon_max=max(lons) + pad,
    )


def context_for_course(course: Course) -> GeoContext:
    from .fetch import ensure_geo_context

    return ensure_geo_context(course)
