"""Forecast horizon estimation for routing and prefetch."""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import datetime, timedelta

from ..geo import distance_nm
from ..schemas import Course

# Match map scrubber prefetch window (apps/web PREFETCH_WINDOW_H).
CHUNK_H = 3
# Cap the *initial* /route wind prefetch; longer courses extend +3h as needed.
INITIAL_ROUTE_HORIZON_H = 24
# Conservative cruise speed so upwind/detour courses don't exhaust forecast mid-route
# and trigger full re-routes (each extend re-runs the whole course).
_ASSUMED_SPEED_KN = 3.5
# Extra headroom multiplier on top of distance/speed estimate.
_HORIZON_PAD = 1.4
_MIN_HORIZON_H = 6
_MAX_HORIZON_H = 168  # 7 days hard cap


def course_distance_nm(course: Course) -> float:
    pts = [course.start, *[m.position for m in course.marks], course.finish]
    total = 0.0
    for a, b in zip(pts, pts[1:], strict=False):
        total += distance_nm(a, b)
    return total


def routing_horizon_h(course: Course) -> int:
    """Hours of forecast to prefetch: ceil(course_nm / 3.5 kn * 1.4), clamped 6..168."""
    dist = max(1.0, course_distance_nm(course))
    hours = math.ceil(dist / _ASSUMED_SPEED_KN * _HORIZON_PAD)
    return int(max(_MIN_HORIZON_H, min(_MAX_HORIZON_H, hours)))


def initial_route_horizon_h(course: Course) -> int:
    """Shorter upfront window for /route; extend path covers the rest."""
    return min(routing_horizon_h(course), INITIAL_ROUTE_HORIZON_H)


def estimate_horizon_h(course: Course) -> int:
    """Alias for routing_horizon_h (kept for older call sites)."""
    return routing_horizon_h(course)


def iter_hour_chunks(
    start: datetime,
    end: datetime,
    chunk_h: int = CHUNK_H,
) -> Iterator[tuple[datetime, datetime]]:
    """Yield [chunk_start, chunk_end] covering [start, end] in chunk_h steps."""
    if end < start:
        start, end = end, start
    if chunk_h < 1:
        chunk_h = 1
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + timedelta(hours=chunk_h))
        yield cursor, chunk_end
        cursor = chunk_end
