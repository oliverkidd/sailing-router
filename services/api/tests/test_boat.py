from __future__ import annotations

import pytest

from app.boat import Boat
from app.navmath import angle_between, magnitude_bearing, signed_diff, vector
from app.polar import PolarTable
from app.schemas import BoatConfig

CSV = """twa/tws,40,52,60,90,120,150,180
6,0,4.6,5.1,5.8,5.5,4.3,3.2
10,0,6.4,6.9,7.5,7.3,6.2,4.9
20,0,7.6,8.2,9.6,10.9,10.8,9.1
"""


@pytest.fixture()
def polar() -> PolarTable:
    return PolarTable.from_csv(CSV)


def test_polar_efficiency_scales_bsp(polar):
    full = Boat(BoatConfig(polar_id="p", polar_efficiency=1.0), polar)
    derated = Boat(BoatConfig(polar_id="p", polar_efficiency=0.8), polar)
    assert derated.bsp(10, 90) == pytest.approx(0.8 * full.bsp(10, 90))


def test_manual_override_changes_target_angle(polar):
    derived = Boat(BoatConfig(polar_id="p"), polar)
    manual = Boat(BoatConfig(polar_id="p", upwind_twa_deg=42.0), polar)
    assert manual.target_upwind_twa(12) == 42.0
    # Derived beat angle comes from VMG, generally not exactly 42.
    assert derived.target_upwind_twa(12) != 42.0


def test_leeway_disabled_by_default(polar):
    boat = Boat(BoatConfig(polar_id="p"), polar)
    assert boat.leeway_deg(20, 6.0) == 0.0
    assert boat.leeway_offset(20, 6.0, twd=0.0, heading=45.0) == 0.0


def test_leeway_capped_and_signed(polar):
    boat = Boat(BoatConfig(polar_id="p", leeway_coeff=5.0, leeway_max_deg=8.0), polar)
    # Large coeff + low bsp saturates at the cap.
    assert boat.leeway_deg(20, 1.0) == 8.0
    # Wind from north (twd=0), heading 045 (starboard tack): downwind side is south,
    # so the track is pushed to a higher heading (toward downwind).
    off = boat.leeway_offset(20, 6.0, twd=0.0, heading=45.0)
    assert off > 0.0
    off_port = boat.leeway_offset(20, 6.0, twd=0.0, heading=315.0)
    assert off_port < 0.0


def test_navmath_roundtrip():
    vx, vy = vector(90.0, 10.0)  # due east at 10 kn
    assert vx == pytest.approx(10.0)
    assert vy == pytest.approx(0.0, abs=1e-9)
    mag, brg = magnitude_bearing(vx, vy)
    assert mag == pytest.approx(10.0)
    assert brg == pytest.approx(90.0)


def test_angle_helpers():
    assert angle_between(10, 350) == pytest.approx(20.0)
    assert signed_diff(10, 350) == pytest.approx(20.0)
    assert signed_diff(350, 10) == pytest.approx(-20.0)
