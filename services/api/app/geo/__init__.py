"""Geodesy + land / depth / exclusion masks."""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import shapely
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
    """Land polygons with a GEOS prepared index.

    Prepared geometries are not thread-safe. ``/route`` fans models across a
    thread pool that shares one GeoContext, so every GEOS call is serialised.
    """

    def __init__(self, polygons: list[Polygon] | None = None):
        polys = polygons or []
        self._mp = MultiPolygon([p for p in polys]) if polys else MultiPolygon()
        self._prepared = prep(self._mp) if not self._mp.is_empty else None
        # ~110 m cells — enough to collapse repeated segment samples.
        self._cell_cache: dict[tuple[int, int], bool] = {}
        self._lock = threading.RLock()

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
        with self._lock:
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
        with self._lock:
            return self._prepared.intersects(seg)

    def contains_xy(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Vectorised point-in-land (lon=x, lat=y). Thread-safe vs prepared queries."""
        if self._prepared is None:
            return np.zeros(np.shape(x), dtype=bool)
        with self._lock:
            return shapely.contains_xy(self._mp, x, y)


@dataclass
class DepthField:
    lats: np.ndarray
    lons: np.ndarray
    depth_m: np.ndarray
    _cell_cache: dict[tuple[int, int], float] = field(
        default_factory=dict, repr=False, compare=False
    )
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def thread_clone(self) -> DepthField:
        """Share raster arrays; private cell cache for a worker thread."""
        return DepthField(self.lats, self.lons, self.depth_m)

    def depth(self, pos: LatLon) -> float:
        """Conservative depth (m, positive-down) at pos.

        Uses the minimum of the surrounding 2×2 cells so a narrow shoal between
        deep samples is not averaged away. Non-finite / missing → 0 (blocks).
        """
        lat, lon = pos
        lats, lons = self.lats, self.lons
        if lat < lats[0] or lat > lats[-1] or lon < lons[0] or lon > lons[-1]:
            return 0.0
        # Plain searchsorted — avoid np.clip wrapper overhead on the hot path.
        i = int(np.searchsorted(lats, lat) - 1)
        j = int(np.searchsorted(lons, lon) - 1)
        if i < 0:
            i = 0
        elif i > lats.size - 2:
            i = lats.size - 2
        if j < 0:
            j = 0
        elif j > lons.size - 2:
            j = lons.size - 2
        key = (i, j)
        with self._lock:
            hit = self._cell_cache.get(key)
            if hit is not None:
                return hit
            d = self.depth_m
            cells = (d[i, j], d[i, j + 1], d[i + 1, j], d[i + 1, j + 1])
            out = 1e9
            for v in cells:
                fv = 0.0 if not np.isfinite(v) or v < 0 else float(v)
                if fv < out:
                    out = fv
            if out > 1e8:
                out = 0.0
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
    # Quantised blocked_point cache (filled lazily). Millions of segment samples
    # on a long race otherwise re-query the same land/depth cells.
    _blocked_point_cache: dict[tuple[int, int], bool] = field(
        default_factory=dict, repr=False, compare=False
    )
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

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

    def thread_clone(self) -> GeoContext:
        """Per-model worker copy: shared land (locked), private depth/point caches."""
        return GeoContext(
            land=self.land,
            depth=self.depth.thread_clone() if self.depth is not None else None,
            exclusions=self.exclusions,
            min_depth_m=self.min_depth_m,
            depth_safety_margin_m=self.depth_safety_margin_m,
            segment_sample_nm=self.segment_sample_nm,
        )

    def in_exclusion(self, pos: LatLon) -> bool:
        if not self.exclusions:
            return False
        pt = Point(pos[1], pos[0])
        return any(poly.covers(pt) for poly in self.exclusions)

    def too_shallow(self, pos: LatLon) -> bool:
        if self.depth is None:
            return False
        return self.depth.too_shallow(pos, self.min_depth_m + self.depth_safety_margin_m)

    def blocked_point(self, pos: LatLon) -> bool:
        # ~22 m cells — fine enough for shore edges, coarse enough to collapse
        # the millions of repeat segment samples on long isochrone races.
        key = (int(pos[0] * 5000.0), int(pos[1] * 5000.0))
        with self._lock:
            hit = self._blocked_point_cache.get(key)
            if hit is not None:
                return hit
            out = self.land.on_land(pos) or self.too_shallow(pos) or self.in_exclusion(pos)
            self._blocked_point_cache[key] = out
            return out

    def blocked_segment(self, a: LatLon, b: LatLon) -> bool:
        if self.land.segment_crosses_land(a, b):
            return True
        dist = distance_nm(a, b)
        # Long chords (mark-line tests) do not need 90 m samples — 0.15 nm is
        # still finer than chart routing and ~3× fewer depth hits.
        step = self.segment_sample_nm if dist <= 2.0 else max(
            self.segment_sample_nm, min(0.15, dist / 40.0)
        )
        steps = max(2, int(dist / step))
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


def _point_set(p: LatLon) -> bool:
    """True when lat/lon is placed (client uses [0,0] as unset sentinel)."""
    return not (p[0] == 0.0 and p[1] == 0.0)


def _course_points(course: Course) -> list[LatLon]:
    pts: list[LatLon] = []
    if _point_set(course.start):
        pts.append(course.start)
    if _point_set(course.finish):
        pts.append(course.finish)
    for m in course.marks:
        if _point_set(m.position):
            pts.append(m.position)
    return pts


def effective_course_margin_deg(
    course: Course, margin_deg: float = COURSE_WIND_MARGIN_DEG
) -> float:
    """Pad for geo/wind bbox: full margin on compact courses, clamped on huge ones."""
    pts = _course_points(course)
    if not pts:
        return margin_deg
    span = max(
        max(p[0] for p in pts) - min(p[0] for p in pts),
        max(p[1] for p in pts) - min(p[1] for p in pts),
    )
    if span >= _LARGE_COURSE_SPAN_DEG:
        return min(margin_deg, _LARGE_COURSE_MARGIN_DEG)
    return margin_deg


def course_bbox(course: Course, margin_deg: float = COURSE_WIND_MARGIN_DEG) -> BBox:
    """Course corridor bbox. Default margin matches COURSE_WIND_MARGIN_DEG (routing + prepare)."""
    pts = _course_points(course)
    if not pts:
        # Fallback: preserve prior shape if every point is the unset sentinel.
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
