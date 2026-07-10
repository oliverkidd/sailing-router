from __future__ import annotations

import math

import pytest

from app.polar import PolarTable

CSV = """twa/tws,40,52,60,90,120,150,180
6,0,4.6,5.1,5.8,5.5,4.3,3.2
10,0,6.4,6.9,7.5,7.3,6.2,4.9
20,0,7.6,8.2,9.6,10.9,10.8,9.1
"""


@pytest.fixture()
def polar() -> PolarTable:
    return PolarTable.from_csv(CSV)


def test_grid_point_exact(polar):
    # Exact grid lookups return the tabulated value.
    assert polar.speed_for(10, 90) == pytest.approx(7.5)
    assert polar.speed_for(20, 120) == pytest.approx(10.9)
    assert polar.speed_for(6, 52) == pytest.approx(4.6)


def test_interpolation_within_tolerance(polar):
    # Midpoint between (10,90)=7.5 and (20,90)=9.6 -> ~8.55 at tws=15.
    assert polar.speed_for(15, 90) == pytest.approx((7.5 + 9.6) / 2, abs=0.05)
    # Midpoint TWA between 90 and 120 at tws=10 -> (7.5+7.3)/2.
    assert polar.speed_for(10, 105) == pytest.approx((7.5 + 7.3) / 2, abs=0.05)


def test_nogo_returns_zero(polar):
    # Below the smallest tabulated TWA (40) is upwind no-go.
    assert polar.speed_for(10, 30) == 0.0
    assert polar.speed_for(10, 0) == 0.0
    # The 40-degree column is all zeros (dead upwind edge).
    assert polar.speed_for(10, 40) == 0.0


def test_clamp_beyond_axes(polar):
    # TWS above max clamps to top row; TWA above 180 folds back.
    assert polar.speed_for(40, 90) == pytest.approx(9.6)
    assert polar.speed_for(10, 200) == polar.speed_for(10, 160)


def test_best_vmg_upwind_is_a_beat_angle(polar):
    twa, vmg = polar.best_vmg_angle(12, upwind=True)
    assert 40 <= twa <= 90
    assert vmg > 0
    # VMG component must not exceed the boat speed at that angle.
    assert vmg <= polar.speed_for(12, twa) + 1e-6


def test_best_vmg_downwind_is_a_run_angle(polar):
    twa, vmg = polar.best_vmg_angle(20, upwind=False)
    assert 90 <= twa <= 180
    assert vmg == pytest.approx(
        polar.speed_for(20, twa) * abs(math.cos(math.radians(twa))), abs=1e-6
    )
