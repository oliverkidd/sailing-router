"""Eval cases.

Short cases are synthetic and tagged ``fast`` so the inner loop never fetches GRIB.
Chart races are simplified geometries inside the local coverage boxes, not the
official sailing instructions. They are feasibility and runtime checks until an
oracle has been cached for that chart.

Splits:
  tune    — knobs may be fit here
  select  — accept or reject a change; never the same weather type as the fit
  confirm — reported after a change is accepted; never used to accept one
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.geo import destination
from eval.wind_synth import (
    ConstantWind,
    DyingWind,
    GradientWind,
    OscillationWind,
    ShiftWind,
    WindSpec,
)

Split = Literal["tune", "select", "confirm"]
GeoMode = Literal["open", "island", "gap", "channel", "chart"]
RefKind = Literal["exact", "astar", "feasibility"]
Protocol = Literal["single", "forecast_update"]

# Open water south-west of Plymouth. Empty land mask, so the coast is irrelevant.
_OPEN = (50.05, -4.50)


@dataclass(frozen=True)
class MarkSpec:
    lat: float
    lon: float
    name: str
    radius_nm: float = 0.15


@dataclass(frozen=True)
class IslandSpec:
    east_nm: float
    north_nm: float


@dataclass(frozen=True)
class EvalCase:
    id: str
    split: Split
    tags: tuple[str, ...]
    start: tuple[float, float]
    finish: tuple[float, float]
    wind: WindSpec
    geo: GeoMode
    ref: RefKind
    description: str
    marks: tuple[MarkSpec, ...] = ()
    min_depth_m: float = 3.0
    fast: bool = False
    protocol: Protocol = "single"
    # Truth is ``wind``. The boat sees this until ``update_after_s``, then the truth.
    forecast_wind: WindSpec | None = None
    update_after_s: float | None = None
    island: IslandSpec | None = None

    @property
    def points(self) -> list[tuple[float, float]]:
        pts = [self.start]
        pts.extend((m.lat, m.lon) for m in self.marks)
        pts.append(self.finish)
        return pts


def _go(origin: tuple[float, float], bearing_deg: float, dist_nm: float) -> tuple[float, float]:
    return destination(origin, bearing_deg, dist_nm)


def _open(
    case_id: str,
    split: Split,
    tags: tuple[str, ...],
    bearing_deg: float,
    dist_nm: float,
    wind: WindSpec,
    ref: RefKind,
    description: str,
    *,
    origin: tuple[float, float] = _OPEN,
    fast: bool = True,
    protocol: Protocol = "single",
    forecast_wind: WindSpec | None = None,
    update_after_s: float | None = None,
    geo: GeoMode = "open",
    island: IslandSpec | None = None,
    marks: tuple[MarkSpec, ...] = (),
    finish: tuple[float, float] | None = None,
) -> EvalCase:
    return EvalCase(
        id=case_id,
        split=split,
        tags=tags,
        start=origin,
        finish=finish if finish is not None else _go(origin, bearing_deg, dist_nm),
        wind=wind,
        geo=geo,
        ref=ref,
        description=description,
        marks=marks,
        fast=fast,
        protocol=protocol,
        forecast_wind=forecast_wind,
        update_after_s=update_after_s,
        island=island,
    )


def _solent(
    case_id: str,
    start: tuple[float, float],
    finish: tuple[float, float],
    twd: float,
    description: str,
    *,
    geo: GeoMode = "chart",
    marks: tuple[MarkSpec, ...] = (),
    tags: tuple[str, ...] = ("solent", "chart"),
) -> EvalCase:
    return EvalCase(
        id=case_id,
        split="tune",
        tags=tags,
        start=start,
        finish=finish,
        marks=marks,
        wind=ConstantWind(tws=12.0, twd=twd),
        geo=geo,
        ref="feasibility",
        description=description,
        fast=False,
    )


def _race(
    case_id: str,
    split: Split,
    tags: tuple[str, ...],
    start: tuple[float, float],
    finish: tuple[float, float],
    marks: tuple[MarkSpec, ...],
    wind: WindSpec,
    description: str,
) -> EvalCase:
    return EvalCase(
        id=case_id,
        split=split,
        tags=tags,
        start=start,
        finish=finish,
        marks=marks,
        wind=wind,
        geo="chart",
        ref="feasibility",
        description=description,
        fast=False,
    )


def _library() -> list[EvalCase]:
    cases: list[EvalCase] = []

    # --- exact, tune: closed form is the reference ---
    for tws in (8.0, 12.0, 16.0):
        cases.append(
            _open(
                f"reach-beam-{tws:.0f}",
                "tune",
                ("exact", "reach"),
                90.0,
                10.0,
                ConstantWind(tws=tws, twd=0.0),
                "exact",
                f"Beam reach, {tws:.0f} kn from the north",
            )
        )
    cases.append(
        _open(
            "reach-close-12",
            "tune",
            ("exact", "reach"),
            70.0,
            10.0,
            ConstantWind(tws=12.0, twd=0.0),
            "exact",
            "Close reach, 12 kn from the north",
        )
    )
    cases.append(
        _open(
            "reach-broad-12",
            "tune",
            ("exact", "reach"),
            130.0,
            10.0,
            ConstantWind(tws=12.0, twd=0.0),
            "exact",
            "Broad reach, 12 kn from the north",
        )
    )
    for tws in (8.0, 12.0, 16.0):
        cases.append(
            _open(
                f"beat-{tws:.0f}",
                "tune",
                ("exact", "beat"),
                0.0,
                8.0,
                ConstantWind(tws=tws, twd=0.0),
                "exact",
                f"Dead upwind, {tws:.0f} kn from the north",
            )
        )
    for tws in (12.0, 16.0):
        cases.append(
            _open(
                f"run-{tws:.0f}",
                "tune",
                ("exact", "run"),
                180.0,
                8.0,
                ConstantWind(tws=tws, twd=0.0),
                "exact",
                f"Dead downwind, {tws:.0f} kn from the north",
            )
        )
    cases.append(
        _open(
            "beat-offset-12",
            "tune",
            ("exact", "beat", "layline"),
            20.0,
            8.0,
            ConstantWind(tws=12.0, twd=0.0),
            "exact",
            "Upwind with the mark 20° off the wind, 12 kn",
        )
    )
    cases.append(
        _open(
            "shift-upwind-12",
            "tune",
            ("exact", "beat", "shift"),
            0.0,
            8.0,
            ShiftWind(tws=12.0, twd0=0.0, twd1=20.0, shift_after_s=1500.0),
            "exact",
            "Upwind, persistent 20° veer after 25 min",
        )
    )

    # --- exact / astar, select: held-out weather ---
    cases.append(
        _open(
            "oscillate-upwind-12",
            "select",
            ("exact", "beat", "oscillation"),
            0.0,
            8.0,
            OscillationWind(tws=12.0, twd_mean=0.0, amplitude_deg=12.0, period_s=180.0),
            "exact",
            "Upwind, ±12° square wave every 3 min",
        )
    )
    cases.append(
        _open(
            "dying-reach-12",
            "select",
            ("exact", "reach", "dying"),
            90.0,
            8.0,
            DyingWind(tws0=16.0, tws1=6.0, twd=0.0, duration_s=5400.0),
            "exact",
            "Beam reach while the breeze dies from 16 to 6 kn",
        )
    )
    cases.append(
        _open(
            "dying-beat-12",
            "select",
            ("exact", "beat", "dying"),
            0.0,
            6.0,
            DyingWind(tws0=16.0, tws1=6.0, twd=0.0, duration_s=5400.0),
            "exact",
            "Upwind while the breeze dies from 16 to 6 kn",
        )
    )
    grad_start = _OPEN
    cases.append(
        _open(
            "gradient-beat-12",
            "select",
            ("astar", "beat", "gradient"),
            0.0,
            8.0,
            GradientWind(
                tws_lo=8.0,
                tws_hi=16.0,
                twd=0.0,
                lon0=grad_start[1],
                span_nm=10.0,
            ),
            "astar",
            "Upwind with more wind to the east",
            origin=grad_start,
        )
    )

    # Island on the rhumb of an eastbound reach. Reference is A*.
    island_center = _go(_OPEN, 180.0, 20.0)
    island_start = _go(island_center, 270.0, 4.0)
    island_finish = _go(island_center, 90.0, 4.0)
    cases.append(
        EvalCase(
            id="island-beam-12",
            split="select",
            tags=("astar", "reach", "island", "headland"),
            start=island_start,
            finish=island_finish,
            wind=ConstantWind(tws=12.0, twd=0.0),
            geo="island",
            ref="astar",
            description="Beam reach with an island across the rhumb line",
            fast=True,
            island=IslandSpec(east_nm=0.7, north_nm=2.4),
        )
    )

    cases.append(
        _open(
            "forecast-beat-12",
            "select",
            ("exact", "beat", "forecast"),
            0.0,
            8.0,
            ShiftWind(tws=12.0, twd0=0.0, twd1=20.0, shift_after_s=1200.0),
            "exact",
            "Upwind; forecast misses a veer that arrives at 20 min",
            protocol="forecast_update",
            forecast_wind=ConstantWind(tws=12.0, twd=0.0),
            update_after_s=1200.0,
        )
    )

    # --- Solent geometry copied from validate_routing (feasibility; legacy gates remain) ---
    cases.append(
        _solent(
            "solent-reach",
            (50.76, -1.42),
            (50.76, -1.34),
            0.0,
            "Mid-Solent beam reach",
        )
    )
    cases.append(
        _solent(
            "solent-upwind",
            (50.76, -1.34),
            (50.76, -1.42),
            270.0,
            "Mid-Solent dead upwind",
        )
    )
    cases.append(
        _solent(
            "solent-cove",
            (50.77, -1.30),
            (50.76, -1.40),
            270.0,
            "Start near Cowes, finish in the channel",
        )
    )
    cases.append(
        _solent(
            "solent-layline",
            (50.762, -1.34),
            (50.75, -1.42),
            270.0,
            "Upwind with an offset start",
        )
    )
    cases.append(
        _solent(
            "solent-pinch",
            (50.76, -1.36),
            (50.76, -1.42),
            270.0,
            "Dead-upwind approach",
        )
    )
    cases.append(
        _solent(
            "solent-gap",
            (50.780, -1.30),
            (50.760, -1.38),
            45.0,
            "Shoal with a southern gap",
            geo="gap",
            tags=("solent", "gap", "headland"),
        )
    )
    cases.append(
        _solent(
            "solent-channel",
            (50.752, -1.340),
            (50.750, -1.355),
            270.0,
            "Narrow channel, mark, then turn around",
            geo="channel",
            marks=(MarkSpec(50.778, -1.340, "M1", radius_nm=0.1),),
            tags=("solent", "channel", "headland"),
        )
    )

    # --- Chart races. Simplified. Feasibility and wall time, not a scored regret. ---
    # SW of the Needles in deep water. A mark due north of the rocks sits behind
    # the Shingles Bank and made the south-westerly approach thrash on a false
    # geometric layline through the shoal.
    needles = MarkSpec(50.662, -1.610, "Needles Fairway")
    st_cath = MarkSpec(50.573, -1.298, "St Catherines")
    bembridge = MarkSpec(50.681, -1.041, "Bembridge Ledge")
    rti_start = (50.772, -1.300)
    rti_finish = (50.770, -1.310)
    rti_winds: tuple[tuple[str, Split, float, str], ...] = (
        ("rti-w", "tune", 270.0, "west"),
        ("rti-ne", "tune", 45.0, "north-east"),
        ("rti-s", "tune", 180.0, "south"),
        ("rti-sw", "select", 225.0, "south-west"),
    )
    for case_id, split, twd, name in rti_winds:
        cases.append(
            _race(
                case_id,
                split,
                ("chart", "rti", "geometry"),
                rti_start,
                rti_finish,
                (needles, st_cath, bembridge),
                ConstantWind(tws=12.0, twd=twd),
                f"Round the Island, simplified, wind from the {name}",
            )
        )
    cases.append(
        _race(
            "rti-depression",
            "confirm",
            ("chart", "rti", "shift"),
            rti_start,
            rti_finish,
            (needles, st_cath, bembridge),
            ShiftWind(tws=18.0, twd0=200.0, twd1=280.0, shift_after_s=4 * 3600.0),
            "Round the Island under a veering depression",
        )
    )

    # Le Palais roadstead, not the town quay (on land / drying).
    belle_start = (47.3479, -3.1428)
    cases.append(
        _race(
            "belle-ile",
            "select",
            ("chart", "france", "island", "headland"),
            belle_start,
            (47.3493, -3.1428),
            (
                MarkSpec(47.240, -3.150, "South"),
                MarkSpec(47.320, -3.280, "West"),
            ),
            ConstantWind(tws=14.0, twd=270.0),
            "Loop of Belle-Ile, simplified",
        )
    )
    cases.append(
        _race(
            "giraglia",
            "confirm",
            ("chart", "france", "med"),
            (43.2746, 6.6360),
            (44.3450, 8.9124),
            (MarkSpec(43.0286, 9.4040, "Giraglia"),),
            ConstantWind(tws=14.0, twd=315.0),
            "Saint-Tropez, Giraglia, Genoa roads, simplified",
        )
    )
    cases.append(
        _race(
            "rorc-channel",
            "confirm",
            ("chart", "channel"),
            (50.7691, -1.3025),
            (50.7704, -1.3000),
            (
                MarkSpec(50.662, -1.610, "Needles"),
                MarkSpec(49.90, -2.20, "Casquets"),
                MarkSpec(50.10, -1.00, "Mid-Channel"),
            ),
            ConstantWind(tws=14.0, twd=225.0),
            "RORC Channel race, simplified",
        )
    )

    # Cowes → Needles → Lizard → Fastnet Rock → Plymouth.
    fastnet_marks = (
        MarkSpec(50.662, -1.610, "Needles"),
        MarkSpec(49.90, -5.20, "Lizard"),
        MarkSpec(51.3986, -9.5995, "Fastnet"),
    )
    cases.append(
        _race(
            "fastnet-sw",
            "confirm",
            ("chart", "fastnet"),
            (50.772, -1.300),
            (50.3604, -4.1423),
            fastnet_marks,
            ConstantWind(tws=15.0, twd=225.0),
            "Fastnet, simplified, steady south-westerly",
        )
    )
    cases.append(
        _race(
            "fastnet-veer",
            "confirm",
            ("chart", "fastnet", "shift"),
            (50.772, -1.300),
            (50.3604, -4.1423),
            fastnet_marks,
            ShiftWind(tws=16.0, twd0=180.0, twd1=270.0, shift_after_s=18 * 3600.0),
            "Fastnet, simplified, southerly veering west",
        )
    )

    aegean_marks = (
        MarkSpec(36.39, 25.43, "Santorini"),
        MarkSpec(35.34, 26.99, "Kasos"),
        MarkSpec(36.55, 27.15, "Kos"),
        MarkSpec(37.0884, 26.8622, "Agathonisi"),
        MarkSpec(37.6108, 24.0700, "Kea"),
    )
    cases.append(
        _race(
            "aegean-600-n",
            "confirm",
            ("chart", "aegean"),
            (37.44, 23.95),
            (37.45, 23.96),
            aegean_marks,
            ConstantWind(tws=15.0, twd=10.0),
            "Aegean 600, simplified, northerly",
        )
    )
    cases.append(
        _race(
            "aegean-600-shift",
            "confirm",
            ("chart", "aegean", "shift"),
            (37.44, 23.95),
            (37.45, 23.96),
            aegean_marks,
            ShiftWind(tws=14.0, twd0=10.0, twd1=40.0, shift_after_s=24 * 3600.0),
            "Aegean 600, simplified, northerly veering after a day",
        )
    )

    middle_marks = (
        MarkSpec(37.9394, 15.3733, "Messina"),
        MarkSpec(38.4936, 14.9707, "Stromboli"),
        MarkSpec(37.9442, 12.3232, "Favignana"),
    )
    cases.append(
        _race(
            "middle-sea-nw",
            "confirm",
            ("chart", "middle-sea"),
            (35.9127, 14.5200),
            (35.9176, 14.5108),
            middle_marks,
            ConstantWind(tws=14.0, twd=315.0),
            "Rolex Middle Sea, simplified, north-westerly",
        )
    )
    cases.append(
        _race(
            "middle-sea-rotate",
            "confirm",
            ("chart", "middle-sea", "shift"),
            (35.9127, 14.5200),
            (35.9176, 14.5108),
            middle_marks,
            ShiftWind(tws=12.0, twd0=270.0, twd1=180.0, shift_after_s=36 * 3600.0),
            "Rolex Middle Sea, simplified, wind backing over the race",
        )
    )
    # --- Robustness: must finish. Not a speed score. ---
    # The course from the editor: west of the Needles, south of the island, east, back in.
    loop_start = (50.0 + 44.183 / 60.0, -(1.0 + 27.782 / 60.0))
    loop_marks = (
        MarkSpec(50.0 + 40.861 / 60.0, -(1.0 + 37.987 / 60.0), "M1", 0.1),
        MarkSpec(50.0 + 34.082 / 60.0, -(1.0 + 19.718 / 60.0), "M2", 0.1),
        MarkSpec(50.0 + 45.770 / 60.0, -(1.0 + 11.534 / 60.0), "M3", 0.1),
    )
    loop_finish = (50.0 + 43.064 / 60.0, -(1.0 + 29.720 / 60.0))
    for twd in (0.0, 45.0, 90.0, 180.0, 225.0, 270.0, 315.0):
        cases.append(
            _race(
                f"robust-solent-loop-{twd:.0f}",
                "confirm",
                ("robust", "chart", "solent", "loop"),
                loop_start,
                loop_finish,
                loop_marks,
                ConstantWind(tws=12.0, twd=twd),
                f"Solent loop from the course editor, wind {twd:.0f}",
            )
        )

    # Legs shorter than the 1 nm tack lockout and the 0.8 nm near-mark window.
    tri = (
        MarkSpec(*_go(_OPEN, 0.0, 0.40), "M1", 0.05),
        MarkSpec(*_go(_OPEN, 120.0, 0.40), "M2", 0.05),
    )
    for twd, name in ((0.0, "beat"), (90.0, "reach"), (180.0, "run")):
        cases.append(
            _open(
                f"robust-triangle-0.4-{name}",
                "confirm",
                ("robust", "triangle", "short"),
                0.0,
                0.40,
                ConstantWind(tws=12.0, twd=twd),
                "feasibility",
                f"0.4 nm triangle, wind {twd:.0f} ({name})",
                marks=tri,
                finish=_go(_OPEN, 240.0, 0.40),
                fast=False,
            )
        )

    # Out and back, so the turn is a hairpin rather than a gentle corner.
    cases.append(
        _open(
            "robust-hairpin-6",
            "confirm",
            ("robust", "hairpin"),
            0.0,
            0.4,
            ConstantWind(tws=12.0, twd=0.0),
            "feasibility",
            "6 nm dead upwind, then back down to a mark beside the start",
            marks=(MarkSpec(*_go(_OPEN, 0.0, 6.0), "M1", 0.1),),
            finish=_go(_OPEN, 90.0, 0.4),
            fast=False,
        )
    )

    # Three long legs: beat, reach, run. Catches a step cap or a stall that
    # short cases never reach.
    m_north = _go(_OPEN, 0.0, 25.0)
    m_east = _go(m_north, 90.0, 25.0)
    cases.append(
        _open(
            "robust-long-25",
            "confirm",
            ("robust", "long"),
            0.0,
            25.0,
            ConstantWind(tws=12.0, twd=0.0),
            "feasibility",
            "25 nm beat, 25 nm reach, 25 nm run",
            marks=(
                MarkSpec(*m_north, "M1", 0.15),
                MarkSpec(*m_east, "M2", 0.15),
            ),
            finish=_go(m_east, 180.0, 25.0),
            fast=False,
        )
    )

    # Rectangle around the same island the beam-reach case uses.
    center = _go(_OPEN, 180.0, 20.0)
    cases.append(
        EvalCase(
            id="robust-island-triangle",
            split="confirm",
            tags=("robust", "island", "triangle"),
            start=_go(center, 270.0, 3.0),
            finish=_go(center, 180.0, 3.0),
            wind=ConstantWind(tws=12.0, twd=0.0),
            geo="island",
            ref="feasibility",
            description="Triangle around a rectangular island",
            marks=(
                MarkSpec(*_go(center, 0.0, 3.0), "N", 0.1),
                MarkSpec(*_go(center, 90.0, 3.0), "E", 0.1),
            ),
            fast=False,
            island=IslandSpec(east_nm=0.7, north_nm=2.4),
        )
    )

    # A few cables across the mid-Solent, where every leg sits inside near_mark.
    sol = (50.760, -1.380)
    cases.append(
        _race(
            "robust-solent-triangle",
            "confirm",
            ("robust", "chart", "solent", "triangle", "short"),
            sol,
            _go(sol, 200.0, 0.35),
            (
                MarkSpec(*_go(sol, 80.0, 0.40), "M1", 0.05),
                MarkSpec(*_go(sol, 150.0, 0.40), "M2", 0.05),
            ),
            ConstantWind(tws=12.0, twd=270.0),
            "0.4 nm triangle in the mid-Solent, westerly",
        )
    )
    cases.append(
        _race(
            "robust-solent-loop-osc",
            "confirm",
            ("robust", "chart", "solent", "loop", "oscillation"),
            loop_start,
            loop_finish,
            loop_marks,
            OscillationWind(tws=12.0, twd_mean=270.0, amplitude_deg=18.0, period_s=240.0),
            "Same Solent loop, westerly flipping 18° every 4 minutes",
        )
    )

    # Five 2 nm legs, each turn 150°. A corner the layline rule was not written for.
    brg = 20.0
    pos = _OPEN
    corners: list[tuple[float, float]] = []
    for _ in range(5):
        pos = _go(pos, brg, 2.0)
        corners.append(pos)
        brg = (brg + 150.0) % 360.0
    cases.append(
        _open(
            "robust-zigzag",
            "confirm",
            ("robust", "zigzag"),
            0.0,
            2.0,
            ConstantWind(tws=10.0, twd=40.0),
            "feasibility",
            "Five 2 nm legs with 150° turns",
            marks=tuple(
                MarkSpec(p[0], p[1], f"Z{i}", 0.08) for i, p in enumerate(corners[:-1], 1)
            ),
            finish=corners[-1],
            fast=False,
        )
    )
    tiny = _OPEN
    cases.append(
        _open(
            "robust-triangle-0.22",
            "confirm",
            ("robust", "triangle", "short"),
            0.0,
            0.22,
            ConstantWind(tws=8.0, twd=10.0),
            "feasibility",
            "0.22 nm triangle, smaller than the finish snap radius",
            marks=(
                MarkSpec(*_go(tiny, 0.0, 0.22), "A", 0.04),
                MarkSpec(*_go(tiny, 130.0, 0.22), "B", 0.04),
            ),
            finish=_go(tiny, 250.0, 0.22),
            fast=False,
        )
    )
    return cases


CASES: tuple[EvalCase, ...] = tuple(_library())
BY_ID: dict[str, EvalCase] = {c.id: c for c in CASES}

SMOKE_IDS: tuple[str, ...] = (
    "reach-beam-12",
    "beat-12",
    "run-12",
    "shift-upwind-12",
    "island-beam-12",
    "forecast-beat-12",
)

# Closed-form cases the A* oracle must match before it is trusted on geometry.
ORACLE_CHECK_IDS: tuple[str, ...] = ("reach-beam-12", "beat-12", "run-12")


def select_cases(suite: str, ids: list[str] | None = None) -> list[EvalCase]:
    if ids:
        missing = [i for i in ids if i not in BY_ID]
        if missing:
            raise KeyError(f"Unknown case id: {', '.join(missing)}")
        return [BY_ID[i] for i in ids]
    if suite == "smoke":
        return [BY_ID[i] for i in SMOKE_IDS]
    if suite == "fast":
        return [c for c in CASES if c.fast]
    if suite == "robust":
        return [c for c in CASES if "robust" in c.tags]
    if suite == "all":
        return list(CASES)
    if suite in ("tune", "select", "confirm"):
        return [c for c in CASES if c.split == suite]
    raise ValueError(f"Unknown suite: {suite}")
