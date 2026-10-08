"""Open-water reference times.

Constant uniform wind: at most one tack or one gybe, on a VMG heading or the
rhumb. That policy is optimal, and the arrival circle is the same radius the
router stops at.

A persistent shift is enumerated: every one-tack schedule up to the shift,
then the constant-wind optimum on the new wind. A fast oscillation and a dying
breeze are integrated. The square-wave reference is the better of holding the
mean-wind headings and tacking on every flip.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.boat import Boat
from app.geo import bearing, distance_nm
from app.navmath import norm360, signed_diff, twa_from
from app.router import Node, _detect_maneuver
from eval.wind_synth import (
    ConstantWind,
    DyingWind,
    OscillationWind,
    ShiftWind,
    SynthWind,
)

_STEP_NM = 0.05
_VMG_CACHE: dict[tuple[int, float], tuple[float, float]] = {}


def vmg_angles(boat: Boat, tws: float) -> tuple[float, float]:
    """Upwind and downwind target TWA, cached. ``best_vmg_angle`` is expensive."""
    key = (id(boat), round(float(tws), 2))
    hit = _VMG_CACHE.get(key)
    if hit is not None:
        return hit
    angles = (boat.target_upwind_twa(tws), boat.target_downwind_twa(tws))
    _VMG_CACHE[key] = angles
    return angles


@dataclass(frozen=True)
class Reference:
    elapsed_s: float
    n_tacks: int
    n_gybes: int
    method: str


def _xy(origin: tuple[float, float], pos: tuple[float, float]) -> tuple[float, float]:
    lat0 = origin[0]
    north = (pos[0] - lat0) * 60.0
    east = (pos[1] - origin[1]) * 60.0 * math.cos(math.radians(lat0))
    return east, north


def _ll(origin: tuple[float, float], east: float, north: float) -> tuple[float, float]:
    lat = origin[0] + north / 60.0
    cos_lat = max(0.2, math.cos(math.radians(origin[0])))
    lon = origin[1] + east / (60.0 * cos_lat)
    return lat, lon


def _unit(heading: float) -> tuple[float, float]:
    r = math.radians(heading)
    return math.sin(r), math.cos(r)


def _uniq(headings: list[float]) -> list[float]:
    out: list[float] = []
    for h in headings:
        h = norm360(h)
        if all(abs(signed_diff(h, o)) > 0.4 for o in out):
            out.append(h)
    return out


def _bsp(boat: Boat, tws: float, heading: float, twd: float) -> float:
    return boat.bsp(tws, twa_from(heading, twd))


def maneuver_penalty(
    boat: Boat,
    prev_heading: float | None,
    prev_twd: float,
    heading: float,
    twd: float,
    when: datetime,
) -> tuple[str, float]:
    if prev_heading is None:
        return "none", 0.0
    twa = twa_from(heading, twd)
    parent = Node(lat=0.0, lon=0.0, t=when, heading=prev_heading, twd=prev_twd)
    kind = _detect_maneuver(parent, heading, twd, twa)
    if kind == "tack":
        return kind, float(boat.config.tack_penalty_s)
    if kind == "gybe":
        return kind, float(boat.config.gybe_penalty_s)
    return kind, 0.0


def time_on_heading(
    boat: Boat,
    tws: float,
    twd: float,
    pos: tuple[float, float],
    heading: float,
    finish: tuple[float, float],
    radius_nm: float,
) -> float | None:
    """Seconds to enter the finish circle on a constant heading, or None if the ray misses."""
    bsp = _bsp(boat, tws, heading, twd)
    if bsp <= 0.05:
        return None
    if distance_nm(pos, finish) <= radius_nm:
        return 0.0
    fx, fy = _xy(pos, finish)
    ux, uy = _unit(heading)
    d_close = fx * ux + fy * uy
    miss2 = fx * fx + fy * fy - d_close * d_close
    if miss2 > radius_nm * radius_nm + 1e-9:
        return None
    d_enter = d_close - math.sqrt(max(0.0, radius_nm * radius_nm - miss2))
    if d_enter < -1e-6:
        return None
    return max(0.0, d_enter) / bsp * 3600.0


def _move(
    pos: tuple[float, float], heading: float, dist_nm: float
) -> tuple[float, float]:
    ux, uy = _unit(heading)
    return _ll(pos, ux * dist_nm, uy * dist_nm)


def _opt_headings(
    boat: Boat,
    tws: float,
    twd: float,
    start: tuple[float, float],
    finish: tuple[float, float],
) -> list[float]:
    up, dn = vmg_angles(boat, tws)
    raw = [
        bearing(start, finish),
        norm360(twd + up),
        norm360(twd - up),
        norm360(twd + dn),
        norm360(twd - dn),
    ]
    return [h for h in _uniq(raw) if _bsp(boat, tws, h, twd) > 0.05]


def formula_time(
    boat: Boat,
    tws: float,
    twd: float,
    start: tuple[float, float],
    finish: tuple[float, float],
    radius_nm: float,
) -> Reference:
    """Analytic VMG / rhumb time. Used to cross-check the geometric sailor."""
    dist = distance_nm(start, finish)
    sail = max(0.0, dist - radius_nm)
    if sail <= 1e-6:
        return Reference(0.0, 0, 0, "formula")
    twa = twa_from(bearing(start, finish), twd)
    up_twa, dn_twa = vmg_angles(boat, tws)
    up_bsp = boat.bsp(tws, up_twa)
    dn_bsp = boat.bsp(tws, dn_twa)
    up_vmg = up_bsp * math.cos(math.radians(up_twa))
    dn_vmg = dn_bsp * abs(math.cos(math.radians(dn_twa)))
    if twa < up_twa - 1e-6:
        phi = math.radians(twa)
        elapsed = sail * math.cos(phi) / up_vmg * 3600.0 + boat.config.tack_penalty_s
        return Reference(elapsed, 1, 0, "formula")
    if twa > dn_twa + 1e-6:
        psi = math.radians(180.0 - twa)
        elapsed = sail * math.cos(psi) / dn_vmg * 3600.0 + boat.config.gybe_penalty_s
        return Reference(elapsed, 0, 1, "formula")
    bsp = boat.bsp(tws, twa)
    return Reference(sail / bsp * 3600.0, 0, 0, "formula")


def optimal_constant(
    boat: Boat,
    tws: float,
    twd: float,
    start: tuple[float, float],
    finish: tuple[float, float],
    radius_nm: float,
    when: datetime,
) -> Reference:
    """Best one-maneuver policy in constant uniform wind."""
    headings = _opt_headings(boat, tws, twd, start, finish)
    best = Reference(math.inf, 0, 0, "exact")

    for h in headings:
        t = time_on_heading(boat, tws, twd, start, h, finish, radius_nm)
        if t is not None and t < best.elapsed_s:
            best = Reference(t, 0, 0, "exact")

    for h1 in headings:
        for h2 in headings:
            if abs(signed_diff(h1, h2)) < 0.4:
                continue
            kind, pen = maneuver_penalty(boat, h1, twd, h2, twd, when)
            if kind not in ("tack", "gybe"):
                continue
            bsp1 = _bsp(boat, tws, h1, twd)
            # Step along h1 until h2 fetches the circle.
            max_nm = distance_nm(start, finish) * 2.5 + 2.0
            sailed = 0.0
            while sailed <= max_nm + 1e-9:
                pos = start if sailed <= 0 else _move(start, h1, sailed)
                rest = time_on_heading(boat, tws, twd, pos, h2, finish, radius_nm)
                if rest is not None:
                    elapsed = sailed / bsp1 * 3600.0 + pen + rest
                    tacks = 1 if kind == "tack" else 0
                    gybes = 1 if kind == "gybe" else 0
                    if elapsed < best.elapsed_s:
                        best = Reference(elapsed, tacks, gybes, "exact")
                    break
                sailed += _STEP_NM
                if distance_nm(pos, finish) > distance_nm(start, finish) + 6.0 and sailed > 1.0:
                    break

    if not math.isfinite(best.elapsed_s):
        raise RuntimeError("constant-wind reference found no route")
    return best


def _wind_sample(wind: SynthWind, t: datetime) -> tuple[float, float]:
    w = wind.wind_at(0.0, 0.0, t)
    if w is None:
        raise RuntimeError("synthetic wind returned None")
    return w


def optimal_shift(
    boat: Boat,
    wind: SynthWind,
    start: tuple[float, float],
    finish: tuple[float, float],
    radius_nm: float,
    t_start: float,
) -> Reference:
    """Enumerate pre-shift VMG schedules, then the constant optimum on the new wind."""
    spec = wind.spec
    if not isinstance(spec, ShiftWind):
        raise TypeError("optimal_shift requires ShiftWind")
    t0 = wind.t0
    shift_at = spec.shift_after_s
    when0 = t0 + timedelta(seconds=t_start)

    def at_const(pos: tuple[float, float], t_abs: float, tws: float, twd: float) -> Reference:
        return optimal_constant(
            boat, tws, twd, pos, finish, radius_nm, t0 + timedelta(seconds=t_abs)
        )

    best = Reference(math.inf, 0, 0, "exact-shift")

    # Finish on the old wind before it shifts.
    pre = at_const(start, t_start, spec.tws, spec.twd0)
    if t_start + pre.elapsed_s <= shift_at + 1.0:
        best = Reference(pre.elapsed_s, pre.n_tacks, pre.n_gybes, "exact-shift")

    remain = shift_at - t_start
    if remain <= 1.0:
        post = at_const(start, shift_at, spec.tws, spec.twd1)
        return _min_ref(best, post)

    headings = _opt_headings(boat, spec.tws, spec.twd0, start, finish)
    switches = [remain]
    tau = 90.0
    while tau < remain - 30.0:
        switches.append(tau)
        tau += 90.0

    for h1 in headings:
        for h2 in headings:
            for switch in switches:
                if abs(signed_diff(h1, h2)) < 0.4 and switch < remain - 1.0:
                    continue
                outcome = _sail_until_shift(
                    boat,
                    start,
                    finish,
                    radius_nm,
                    h1,
                    h2,
                    switch,
                    remain,
                    spec.tws,
                    spec.twd0,
                    when0,
                )
                if outcome is None:
                    continue
                pos, elapsed, hit, tacks, gybes = outcome
                if hit:
                    best = _min_ref(best, Reference(elapsed, tacks, gybes, "exact-shift"))
                    continue
                post = at_const(pos, t_start + elapsed, spec.tws, spec.twd1)
                # One extra tack may be counted inside `post`. Add the pre-shift maneuvers.
                best = _min_ref(
                    best,
                    Reference(
                        elapsed + post.elapsed_s,
                        tacks + post.n_tacks,
                        gybes + post.n_gybes,
                        "exact-shift",
                    ),
                )
    if not math.isfinite(best.elapsed_s):
        raise RuntimeError("shift reference found no route")
    return best


def _min_ref(a: Reference, b: Reference) -> Reference:
    return a if a.elapsed_s <= b.elapsed_s else b


def _sail_until_shift(
    boat: Boat,
    start: tuple[float, float],
    finish: tuple[float, float],
    radius_nm: float,
    h1: float,
    h2: float,
    switch_s: float,
    remain_s: float,
    tws: float,
    twd: float,
    when: datetime,
) -> tuple[tuple[float, float], float, bool, int, int] | None:
    """Sail h1 until switch_s, pay the maneuver, sail h2 until the shift.

    Returns position, elapsed, whether the finish circle was entered, tack count, gybe count.
    """
    bsp1 = _bsp(boat, tws, h1, twd)
    if bsp1 <= 0.05:
        return None
    kind, pen = ("none", 0.0)
    if abs(signed_diff(h1, h2)) >= 0.4:
        kind, pen = maneuver_penalty(boat, h1, twd, h2, twd, when)
    tacks = 1 if kind == "tack" else 0
    gybes = 1 if kind == "gybe" else 0

    hit1 = time_on_heading(boat, tws, twd, start, h1, finish, radius_nm)
    if hit1 is not None and hit1 <= switch_s:
        return _move(start, h1, bsp1 * hit1 / 3600.0), hit1, True, 0, 0

    pos = _move(start, h1, bsp1 * switch_s / 3600.0)
    t = switch_s + pen
    if t >= remain_s:
        # Penalty runs past the shift; hand the position at the shift to the caller
        # by not sailing h2. If the penalty itself crosses the shift, freeze at switch.
        return pos, remain_s, False, tacks, gybes

    bsp2 = _bsp(boat, tws, h2, twd)
    if bsp2 <= 0.05:
        return None
    budget = remain_s - t
    hit2 = time_on_heading(boat, tws, twd, pos, h2, finish, radius_nm)
    if hit2 is not None and hit2 <= budget:
        dist = bsp2 * hit2 / 3600.0
        return _move(pos, h2, dist), t + hit2, True, tacks, gybes
    pos2 = _move(pos, h2, bsp2 * budget / 3600.0)
    return pos2, remain_s, False, tacks, gybes


def simulate_uniform(
    boat: Boat,
    wind: SynthWind,
    start: tuple[float, float],
    finish: tuple[float, float],
    radius_nm: float,
    t_start: float,
    *,
    chase: bool,
) -> Reference:
    """Integrate a spatially uniform, time-varying wind.

    ``chase=False`` holds the headings that are optimal for the mean (or the
    instantaneous direction, for a dying breeze with fixed TWD — same thing).
    ``chase=True`` retacks onto the lifted board whenever the wind flips.
    """
    spec = wind.spec
    if isinstance(spec, OscillationWind):
        guide = ConstantWind(tws=spec.tws, twd=spec.twd_mean)
    elif isinstance(spec, DyingWind):
        guide = ConstantWind(tws=max(spec.tws0, spec.tws1), twd=spec.twd)
    else:
        raise TypeError("simulate_uniform supports oscillation and dying winds")

    # Heading plan from the guide wind: the constant-wind optimum's two headings
    # are recovered by asking optimal_constant... we re-derive the pair here and
    # follow the long tack, then the fetching tack, using *actual* boat speed.
    tws_g, twd_g = guide.tws, guide.twd
    headings = _opt_headings(boat, tws_g, twd_g, start, finish)
    # Prefer a direct fetch on the guide wind.
    direct = [
        (h, time_on_heading(boat, tws_g, twd_g, start, h, finish, radius_nm)) for h in headings
    ]
    direct_hits = [(h, t) for h, t in direct if t is not None]
    plan: list[float]
    if direct_hits and not chase:
        plan = [min(direct_hits, key=lambda ht: ht[1])[0]]
    else:
        plan = _long_then_lay(boat, tws_g, twd_g, start, finish, radius_nm, headings)

    dt = 15.0
    t = t_start
    pos = start
    prev_h: float | None = None
    prev_twd = twd_g
    tacks = 0
    gybes = 0
    hold = plan[0]
    lay = plan[-1]
    # A 10 nm leg at 2 kn is 5 hours. A day is a hard stop.
    limit = t_start + 24 * 3600.0
    when = wind.t0
    while t < limit:
        if distance_nm(pos, finish) <= radius_nm:
            return Reference(t - t_start, tacks, gybes, "exact-sim")
        now = when + timedelta(seconds=t)
        tws, twd = _wind_sample(wind, now)
        if chase or isinstance(spec, DyingWind):
            # Dying breeze keeps its direction, so the greedy VMG heading at the
            # current speed is optimal. Chasing an oscillation retacks every flip.
            live = _opt_headings(boat, tws, twd, pos, finish)
            choice = _choose_heading(boat, tws, twd, pos, finish, radius_nm, live, prev_h)
        else:
            # Hold the mean-wind plan. The tack onto the layline is decided on the
            # mean wind, not the live wiggle.
            if hold != lay and time_on_heading(
                boat, tws_g, twd_g, pos, lay, finish, radius_nm
            ):
                choice = lay
            else:
                choice = hold
        kind, pen = maneuver_penalty(boat, prev_h, prev_twd, choice, twd, now)
        if pen > 0:
            t += pen
            if kind == "tack":
                tacks += 1
            elif kind == "gybe":
                gybes += 1
            prev_h = choice
            prev_twd = twd
            continue
        bsp = _bsp(boat, tws, choice, twd)
        if bsp <= 0.05:
            live = _opt_headings(boat, tws, twd, pos, finish)
            if not live:
                raise RuntimeError("no sailable heading in the simulator")
            choice = min(live, key=lambda h: abs(signed_diff(h, bearing(pos, finish))))
            bsp = _bsp(boat, tws, choice, twd)
        step = dt
        dist = bsp * step / 3600.0
        hit = time_on_heading(boat, tws, twd, pos, choice, finish, radius_nm)
        if hit is not None and hit <= step:
            return Reference(t + hit - t_start, tacks, gybes, "exact-sim")
        pos = _move(pos, choice, dist)
        t += step
        prev_h = choice
        prev_twd = twd
    raise RuntimeError("time-varying reference did not reach the finish")


def _long_then_lay(
    boat: Boat,
    tws: float,
    twd: float,
    start: tuple[float, float],
    finish: tuple[float, float],
    radius_nm: float,
    headings: list[float],
) -> list[float]:
    """Return [first, second] for the quickest one-maneuver plan, or [only]."""
    best_h = headings[0]
    best_t = math.inf
    for h in headings:
        t = time_on_heading(boat, tws, twd, start, h, finish, radius_nm)
        if t is not None and t < best_t:
            best_t = t
            best_h = h
    if math.isfinite(best_t):
        return [best_h]
    # Fall back to the heading closest to the mark, then its partner.
    brg = bearing(start, finish)
    first = min(headings, key=lambda h: abs(signed_diff(h, brg)))
    rest = [h for h in headings if abs(signed_diff(h, first)) > 1.0]
    if not rest:
        return [first]
    second = min(rest, key=lambda h: abs(signed_diff(h, brg)))
    return [first, second]


def _choose_heading(
    boat: Boat,
    tws: float,
    twd: float,
    pos: tuple[float, float],
    finish: tuple[float, float],
    radius_nm: float,
    headings: list[float],
    prev: float | None,
) -> float:
    fetching = []
    for h in headings:
        t = time_on_heading(boat, tws, twd, pos, h, finish, radius_nm)
        if t is not None:
            fetching.append((t, h))
    if fetching:
        return min(fetching)[1]
    brg = bearing(pos, finish)
    ranked = sorted(headings, key=lambda h: abs(signed_diff(h, brg)))
    if prev is None:
        return ranked[0]
    # Prefer staying on the current board when the two choices are close.
    if abs(signed_diff(ranked[0], prev)) > 1.0 and len(ranked) > 1:
        if abs(signed_diff(ranked[1], brg)) - abs(signed_diff(ranked[0], brg)) < 8.0:
            if abs(signed_diff(ranked[1], prev)) < abs(signed_diff(ranked[0], prev)):
                return ranked[1]
    return ranked[0]


def best_uniform_sim(
    boat: Boat,
    wind: SynthWind,
    start: tuple[float, float],
    finish: tuple[float, float],
    radius_nm: float,
    t_start: float,
) -> Reference:
    """Lower of the hold-the-mean policy and the chase-every-shift policy."""
    held = simulate_uniform(
        boat, wind, start, finish, radius_nm, t_start, chase=False
    )
    if isinstance(wind.spec, DyingWind):
        # Direction is fixed, so chasing and holding are the same policy.
        return held
    chased = simulate_uniform(
        boat, wind, start, finish, radius_nm, t_start, chase=True
    )
    return _min_ref(held, chased)


def reference_leg(
    boat: Boat,
    wind: SynthWind,
    start: tuple[float, float],
    finish: tuple[float, float],
    radius_nm: float,
    t_start: float,
) -> Reference:
    spec = wind.spec
    when = wind.t0 + timedelta(seconds=t_start)
    if isinstance(spec, ConstantWind):
        return optimal_constant(boat, spec.tws, spec.twd, start, finish, radius_nm, when)
    if isinstance(spec, ShiftWind):
        return optimal_shift(boat, wind, start, finish, radius_nm, t_start)
    if isinstance(spec, OscillationWind | DyingWind):
        return best_uniform_sim(boat, wind, start, finish, radius_nm, t_start)
    raise TypeError(f"No closed-form reference for wind {spec.kind}")


def reference_course(
    boat: Boat,
    wind: SynthWind,
    points: list[tuple[float, float]],
    radii: list[float],
) -> Reference:
    """Sum leg references. The next leg starts on the mark, matching the router's snap."""
    total = 0.0
    tacks = 0
    gybes = 0
    t_cursor = 0.0
    method = "exact"
    for i in range(len(points) - 1):
        ref = reference_leg(boat, wind, points[i], points[i + 1], radii[i], t_cursor)
        total += ref.elapsed_s
        tacks += ref.n_tacks
        gybes += ref.n_gybes
        t_cursor += ref.elapsed_s
        method = ref.method
    return Reference(total, tacks, gybes, method)
