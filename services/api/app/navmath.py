"""Pure angle / vector helpers shared by boat, metocean, and router.

Angle conventions (compass): 0 deg = North, 90 = East, clockwise. Speeds in knots.
A velocity vector (vx, vy) uses vx = East component, vy = North component.
"""

from __future__ import annotations

import math


def norm360(deg: float) -> float:
    return deg % 360.0


def signed_diff(a: float, b: float) -> float:
    """Signed smallest angle a-b in (-180, 180]."""
    d = (a - b + 180.0) % 360.0 - 180.0
    return d + 360.0 if d <= -180.0 else d


def angle_between(a: float, b: float) -> float:
    """Absolute 0..180 angle between two compass bearings."""
    return abs(signed_diff(a, b))


def twa_from(heading: float, twd: float) -> float:
    """True wind angle 0..180 given heading and TWD (direction wind blows FROM)."""
    return angle_between(heading, twd)


def vector(bearing: float, speed: float) -> tuple[float, float]:
    """Compass bearing + speed -> (east, north) velocity components."""
    r = math.radians(bearing)
    return speed * math.sin(r), speed * math.cos(r)


def magnitude_bearing(vx: float, vy: float) -> tuple[float, float]:
    """(east, north) velocity -> (magnitude, compass bearing)."""
    mag = math.hypot(vx, vy)
    brg = norm360(math.degrees(math.atan2(vx, vy)))
    return mag, brg
