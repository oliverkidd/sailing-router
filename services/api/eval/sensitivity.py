"""One-factor sensitivity screen.

Varies one RouterParam at a time on the fast suite (or a smaller set) and
records how far select-set regret moves. A knob is inert only when the cases
actually exercise it and the regret span stays under half a percentage point.
``depth_safety_margin_m`` is frozen. Waypoint knobs are not called inert unless
an island or chart case was in the run.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.schemas import RouterParamsOverride
from app.tuning import _field_bounds
from eval.cases import select_cases
from eval.params import FROZEN_PARAMS, WAYPOINT_PARAMS, params_snapshot, product_params
from eval.run import execute
from eval.score import CaseScore, load_boat


def _clip(name: str, value: float) -> float:
    info = RouterParamsOverride.model_fields.get(name)
    if info is None:
        return value
    lo, hi = _field_bounds(info)
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def trial_values(name: str, default: object) -> list[object]:
    if name in FROZEN_PARAMS:
        return []
    if name == "waypoint_guidance":
        return [0, 1]
    if isinstance(default, bool):
        return []
    if isinstance(default, int):
        raw = [max(1, int(default * 0.5)), int(default), int(round(default * 1.5))]
        vals: list[object] = []
        for v in raw:
            clipped = int(_clip(name, float(v)))
            if clipped not in vals:
                vals.append(clipped)
        return [v for v in vals if v != default]
    if isinstance(default, float):
        raw_f = [default * 0.5, default, default * 1.5]
        out: list[object] = []
        for v in raw_f:
            clipped_f = _clip(name, v)
            if all(abs(clipped_f - float(o)) > 1e-9 for o in out):
                out.append(clipped_f)
        return [v for v in out if abs(float(v) - default) > 1e-9]
    return []


def _median_regret(scores: list[CaseScore], split: str) -> float | None:
    vals = [
        s.regret
        for s in scores
        if s.split == split and s.status == "ok" and s.feasible and s.regret is not None
    ]
    if not vals:
        return None
    ordered = sorted(float(v) for v in vals)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _exercised(name: str, scores: list[CaseScore]) -> bool:
    if name in FROZEN_PARAMS:
        return False
    if name in WAYPOINT_PARAMS:
        return any(
            "island" in (s.extra or {}) or s.id.startswith(("island", "solent", "rti", "belle"))
            for s in scores
        ) or any(s.tag in ("land", "depth") for s in scores)
    return True


def screen(
    scores_at_default: list[CaseScore],
    *,
    names: list[str] | None = None,
    suite: str = "fast",
    ids: list[str] | None = None,
) -> dict[str, Any]:
    boat = load_boat()
    cases = select_cases(suite, ids)
    default_params = product_params()
    base_median = _median_regret(scores_at_default, "select")
    report: dict[str, Any] = {}
    fields = list(params_snapshot(default_params))
    if names:
        fields = [n for n in fields if n in names]
    for name in fields:
        default = getattr(default_params, name)
        values = trial_values(name, default)
        exercised = _exercised(name, scores_at_default) or any(
            c.geo in ("island", "chart", "gap", "channel") for c in cases
        )
        if name in WAYPOINT_PARAMS and not any(c.geo != "open" for c in cases):
            exercised = False
        entry: dict[str, Any] = {
            "default": default,
            "exercised": exercised and name not in FROZEN_PARAMS,
            "frozen": name in FROZEN_PARAMS,
            "trials": [],
        }
        medians: list[float] = []
        if base_median is not None:
            medians.append(base_median)
        feasibility_changed = False
        for value in values:
            trial_scores = execute(cases, boat, product_params(**{name: value}))
            med = _median_regret(trial_scores, "select")
            entry["trials"].append(
                {
                    "value": value,
                    "median_select_regret": med,
                    "feasible": sum(1 for s in trial_scores if s.feasible and s.status == "ok"),
                    "n": len(trial_scores),
                }
            )
            if med is not None:
                medians.append(med)
            base_fail = {s.id for s in scores_at_default if not s.feasible or s.status != "ok"}
            trial_fail = {s.id for s in trial_scores if not s.feasible or s.status != "ok"}
            if base_fail != trial_fail:
                feasibility_changed = True
        span = (max(medians) - min(medians)) if len(medians) >= 2 else 0.0
        entry["span"] = span
        entry["feasibility_changed"] = feasibility_changed
        entry["inert"] = (
            entry["exercised"]
            and not entry["frozen"]
            and not feasibility_changed
            and span < 0.005
        )
        report[name] = entry
    return report


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="One-factor router sensitivity")
    parser.add_argument("--suite", default="fast")
    parser.add_argument("--ids", default="", help="Comma-separated case ids")
    parser.add_argument("--params", default="", help="Comma-separated RouterParams names")
    parser.add_argument("--baseline", type=Path, default=None, help="results.json to reuse as the default")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    ids = [p.strip() for p in args.ids.split(",") if p.strip()] or None
    names = [p.strip() for p in args.params.split(",") if p.strip()] or None
    cases = select_cases(args.suite, ids)
    boat = load_boat()
    if args.baseline is not None:
        payload = json.loads(args.baseline.read_text(encoding="utf-8"))
        wanted = {c.id for c in cases}
        default_scores = [CaseScore(**row) for row in payload["cases"] if row["id"] in wanted]
    else:
        default_scores = execute(cases, boat, product_params())
    result = screen(default_scores, names=names, suite=args.suite, ids=ids)
    out = args.out
    if out is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        from app.config import CACHE_DIR

        out = CACHE_DIR / "routing_eval" / f"sensitivity-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    inert = [name for name, row in result.items() if row.get("inert")]
    print(f"wrote {out}")
    print(f"inert ({len(inert)}): {', '.join(inert) if inert else '(none)'}")
    ranked = sorted(result.items(), key=lambda kv: kv[1].get("span") or 0.0, reverse=True)
    for name, row in ranked[:12]:
        if row.get("frozen"):
            continue
        print(f"  {name}: span={row.get('span')} exercised={row.get('exercised')}")


if __name__ == "__main__":
    main()
