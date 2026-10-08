"""Run the routing eval.

From ``services/api``:

  .venv/bin/python -m eval.run --suite smoke
  .venv/bin/python -m eval.run --suite fast
  .venv/bin/python -m eval.run --ids reach-beam-12,beat-12
  .venv/bin/python -m eval.run --check-oracle
  .venv/bin/python -m eval.run --legacy
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.boat import Boat
from app.config import CACHE_DIR
from app.router import RouterParams
from eval.cases import BY_ID, ORACLE_CHECK_IDS, EvalCase, select_cases
from eval.gates import run_legacy_validate, self_check
from eval.oracle_astar import astar_course
from eval.oracle_exact import formula_time, optimal_constant, optimal_shift
from eval.params import DEPART, params_snapshot, product_params
from eval.report import write_report
from eval.score import CaseScore, build_geo, load_boat, run_case
from eval.wind_synth import ConstantWind, ShiftWind, build_wind

RUNS = CACHE_DIR / "routing_eval" / "runs"
ORACLE_REL = 0.012
FORMULA_REL = 0.015


def execute(cases: list[EvalCase], boat: Boat, params: RouterParams) -> list[CaseScore]:
    scores: list[CaseScore] = []
    for case in cases:
        print(f"  {case.id} ...", flush=True)
        score = run_case(case, boat, params)
        scores.append(score)
        regret = "—" if score.regret is None else f"{score.regret * 100:+.1f}%"
        feas = "ok" if score.feasible else "FAIL"
        if score.status != "ok":
            feas = "ERROR"
        print(
            f"    {feas:5}  regret={regret:>8}  "
            f"router={_secs(score.router_elapsed_s)}  ref={_secs(score.ref_s)}  "
            f"route_time={score.router_runtime_s:.2f}s  tag={score.tag}"
            + (f"  {score.error}" if score.error else "")
            + (f"  fails={','.join(score.fails)}" if score.fails and score.status == "ok" else ""),
            flush=True,
        )
    return scores


def _secs(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}s"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def summary_of(scores: list[CaseScore]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for split in ("tune", "select", "confirm"):
        rows = [s for s in scores if s.split == split]
        if not rows:
            continue
        regs = [float(s.regret) for s in rows if s.regret is not None and s.feasible]
        out[split] = {
            "n": len(rows),
            "feasible": sum(1 for s in rows if s.feasible and s.status == "ok"),
            "errors": sum(1 for s in rows if s.status != "ok"),
            "median_regret": _median(regs),
            "worst_regret": max(regs) if regs else None,
            "router_runtime_s": round(sum(s.router_runtime_s for s in rows), 3),
        }
    return out


def _git_sha() -> str:
    try:
        root = Path(__file__).resolve().parents[3]
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return ""


def check_oracle(boat: Boat, params: RouterParams) -> dict[str, Any]:
    """A* must match the geometric optimum, and that optimum must match the formula."""
    rows: list[dict[str, Any]] = []
    ok = True
    for case_id in ORACLE_CHECK_IDS:
        case = BY_ID[case_id]
        if not isinstance(case.wind, ConstantWind):
            raise RuntimeError(f"{case_id} is not a constant-wind oracle check")
        wind = build_wind(case.wind, DEPART)
        geo = build_geo(case, params)
        radius = float(params.finish_snap_radius_nm)
        exact = optimal_constant(
            boat,
            case.wind.tws,
            case.wind.twd,
            case.start,
            case.finish,
            radius,
            DEPART,
        )
        formula = formula_time(
            boat, case.wind.tws, case.wind.twd, case.start, case.finish, radius
        )
        astar = astar_course(
            boat,
            wind,
            geo,
            [case.start, case.finish],
            [radius],
            DEPART,
            step_nm=0.1,
            cell_nm=0.25,
            heading_step_deg=360.0,
            max_expansions=150_000,
        )
        formula_rel = (formula.elapsed_s - exact.elapsed_s) / exact.elapsed_s
        astar_rel = (astar.elapsed_s - exact.elapsed_s) / exact.elapsed_s
        row_ok = abs(formula_rel) <= FORMULA_REL and abs(astar_rel) <= ORACLE_REL
        ok = ok and row_ok
        rows.append(
            {
                "id": case_id,
                "exact_s": exact.elapsed_s,
                "formula_s": formula.elapsed_s,
                "astar_s": astar.elapsed_s,
                "formula_rel": formula_rel,
                "astar_rel": astar_rel,
                "astar_method": astar.method,
                "ok": row_ok,
            }
        )
        print(
            f"  oracle {case_id}: exact={exact.elapsed_s:.1f}s "
            f"formula={formula.elapsed_s:.1f}s ({formula_rel * 100:+.2f}%) "
            f"astar={astar.elapsed_s:.1f}s ({astar_rel * 100:+.2f}%) "
            f"{'OK' if row_ok else 'MISMATCH'}",
            flush=True,
        )

    # A shift of zero degrees is the constant-wind problem. The enumerator must agree.
    start = BY_ID["beat-12"].start
    finish = BY_ID["beat-12"].finish
    radius = float(params.finish_snap_radius_nm)
    plain = optimal_constant(boat, 12.0, 0.0, start, finish, radius, DEPART)
    shifted = optimal_shift(
        boat,
        build_wind(ShiftWind(tws=12.0, twd0=0.0, twd1=0.0, shift_after_s=600.0), DEPART),
        start,
        finish,
        radius,
        0.0,
    )
    shift_rel = (shifted.elapsed_s - plain.elapsed_s) / plain.elapsed_s
    shift_ok = abs(shift_rel) <= 0.02
    ok = ok and shift_ok
    print(
        f"  oracle zero-shift: constant={plain.elapsed_s:.1f}s "
        f"enum={shifted.elapsed_s:.1f}s ({shift_rel * 100:+.2f}%) "
        f"{'OK' if shift_ok else 'MISMATCH'}",
        flush=True,
    )
    return {"ok": ok, "cases": rows, "zero_shift_rel": shift_rel, "zero_shift_ok": shift_ok}


def _run_external(cases: list[EvalCase], boat: Boat, scores: list[CaseScore]) -> list[dict[str, Any]]:
    from app.config import CACHE_DIR
    from eval.external import available, compare, route_case, write_polar

    if not available():
        raise SystemExit(
            "weatherrouting is not installed. From services/api:\n"
            "  .venv/bin/python -m pip install weatherrouting"
        )
    polar_path = write_polar(boat, CACHE_DIR / "routing_eval" / "sample_orc.pol")
    by_id = {s.id: s for s in scores}
    rows: list[dict[str, Any]] = []
    print(f"libweatherrouting  ({len(cases)} cases)", flush=True)
    for case in cases:
        print(f"  {case.id} ...", flush=True)
        row = route_case(case, boat, polar_path=polar_path)
        ours = by_id.get(case.id)
        gaps = compare(
            ours.router_elapsed_s if ours and ours.feasible else None,
            row,
            ours.ref_s if ours and ours.tag == "ok" else None,
        )
        row = {"id": case.id, **row, **gaps}
        rows.append(row)
        elapsed = "—" if row["elapsed_s"] is None else f"{row['elapsed_s']:.0f}s"
        feas = "ok" if row["feasible"] else "FAIL"
        gap = row.get("our_minus_theirs_rel")
        gap_s = "—" if gap is None else f"{gap * 100:+.1f}%"
        print(
            f"    {feas:5}  their_time={elapsed:>8}  ours_vs_theirs={gap_s:>8}  "
            f"cpu={row['runtime_s']:.1f}s"
            + (f"  {','.join(row['fails'])}" if row["fails"] else ""),
            flush=True,
        )
    return rows


def _payload(
    suite: str,
    params: RouterParams,
    scores: list[CaseScore],
    oracle: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "created_utc": datetime.now(UTC).isoformat(),
        "git": _git_sha(),
        "suite": suite,
        "params": params_snapshot(params),
        "oracle_check": oracle,
        "summary": summary_of(scores),
        "cases": [s.to_dict() for s in scores],
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    # The router logs every leg at INFO. The table above is the eval output.
    logging.getLogger("app").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description="Score the sailing router on fixed cases")
    parser.add_argument(
        "--suite",
        default="smoke",
        choices=("smoke", "fast", "tune", "select", "confirm", "robust", "all"),
    )
    parser.add_argument("--ids", default="", help="Comma-separated case ids, instead of a suite")
    parser.add_argument("--check-oracle", action="store_true", help="A* vs closed form only")
    parser.add_argument("--skip-oracle", action="store_true")
    parser.add_argument("--self-check", action="store_true", help="Gate logic only")
    parser.add_argument("--legacy", action="store_true", help="Also run validate_routing.py --acceptance")
    parser.add_argument(
        "--external",
        action="store_true",
        help="Also run libweatherrouting's isochrone on the same cases",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    argv = argv if argv is not None else sys.argv[1:]
    explicit_suite = any(a == "--suite" or a.startswith("--suite=") for a in argv)

    do_self = args.self_check or (args.suite == "smoke" and not args.ids)
    if do_self:
        print("gate self-check", flush=True)
        self_check()
        print("  OK", flush=True)
    only_self = args.self_check and not explicit_suite and not args.ids and not args.check_oracle
    if only_self:
        return 0

    boat = load_boat()
    params = product_params()
    oracle: dict[str, Any] | None = None
    do_oracle = args.check_oracle or (args.suite == "smoke" and not args.skip_oracle and not args.ids)
    if do_oracle:
        print("oracle check", flush=True)
        oracle = check_oracle(boat, params)
        if not oracle["ok"]:
            print("oracle check failed", flush=True)
            return 1
    only_oracle = args.check_oracle and not explicit_suite and not args.ids
    if only_oracle:
        return 0

    ids = [p.strip() for p in args.ids.split(",") if p.strip()] or None
    cases = select_cases(args.suite if ids is None else "all", ids)
    print(f"router  ({len(cases)} cases)", flush=True)
    scores = execute(cases, boat, params)
    external_rows = None
    if args.external:
        external_rows = _run_external(cases, boat, scores)
    payload = _payload(args.suite if ids is None else "ids", params, scores, oracle)
    if external_rows is not None:
        payload["external"] = external_rows

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or (RUNS / f"{args.suite}-{stamp}" / "results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    report = write_report(payload, out.with_name("report.md"))
    print(f"wrote {out}")
    print(f"wrote {report}")
    for split, stats in payload["summary"].items():
        med = stats["median_regret"]
        med_s = "—" if med is None else f"{med * 100:+.1f}%"
        print(
            f"  {split}: {stats['feasible']}/{stats['n']} feasible, "
            f"median regret {med_s}, router cpu {stats['router_runtime_s']:.1f}s"
        )

    if args.legacy:
        print("legacy validate_routing.py", flush=True)
        code, tail = run_legacy_validate(acceptance=True)
        print(tail)
        if code != 0:
            return code

    if any(s.status != "ok" for s in scores):
        return 1
    # A feasibility case that never reaches the finish is a failed route, even
    # when there is no proved time to score it against.
    missed = [s for s in scores if not s.feasible and s.method in ("", "none")]
    if args.suite == "robust" and missed:
        print("robustness: a course did not finish")
        for s in missed:
            print(f"  {s.id} fails={','.join(s.fails)}")
        return 1
    # A router that beats a proved reference (steady wind or one shift) means that
    # reference is wrong. A faster time than a hand-simulated policy does not.
    beaten = [
        s
        for s in scores
        if s.regret is not None
        and s.regret < -ORACLE_REL
        and s.method in ("exact", "exact-shift", "formula")
    ]
    if beaten:
        print("router beat the reference (reference is not trustworthy):")
        for s in beaten:
            print(f"  {s.id} regret={s.regret:.3f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
