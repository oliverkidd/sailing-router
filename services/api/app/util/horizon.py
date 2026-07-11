"""Forecast horizon estimation for routing and prefetch."""

from __future__ import annotations

import math

from ..geo import distance_nm
from ..schemas import Course

_ASSUMED_SLOW_SPEED_KN = 6.0
_SAFETY_MARGIN = 1.5
_MIN_HORIZON_H = 48
_MAX_HORIZON_H = 384  # Open-Meteo max ~16 days


def course_distance_nm(course: Course) -> float:
    pts = [course.start, *[m.position for m in course.marks], course.finish]
    total = 0.0
    for a, b in zip(pts, pts[1:], strict=False):
        total += distance_nm(a, b)
    return total


def estimate_horizon_h(course: Course) -> int:
    """Conservative horizon: assume slow boat + safety margin."""
    dist = max(1.0, course_distance_nm(course))
    hours = math.ceil((dist / _ASSUMED_SLOW_SPEED_KN) * _SAFETY_MARGIN)
    return int(max(_MIN_HORIZON_H, min(_MAX_HORIZON_H, hours)))


_ROUTE_MIN_HORIZON_H = 168  # 7 days — minimum for routing + Open-Meteo prefetch


def routing_horizon_h(course: Course) -> int:
    """Horizon used for routing and prefetch (course estimate, at least 7 days)."""
    return max(estimate_horizon_h(course), _ROUTE_MIN_HORIZON_H)
