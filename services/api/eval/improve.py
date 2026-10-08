"""Accept or reject one parameter move at a time.

This does not edit ``router.py``. A kept move is an override on top of the
product defaults, written to ``best_overrides.json``. Baking a kept override
into the source is a separate change, judged by the same gate.

  python -m eval.improve --dry-run
  python -m eval.improve --baseline runs/.../results.json --moves moves.json --suite fast

``moves.json`` is a list of objects, each a partial RouterParams override:

  [{"heading_step_deg": 5.0}, {"max_commit_s": 1800}]
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import CACHE_DIR
from eval.cases import select_cases
from eval.gates import decide
from eval.params import FROZEN_PARAMS, product_params
from eval.run import execute
from eval.score import CaseScore, load_boat

JOURNAL = CACHE_DIR / "routing_eval" / "journal.jsonl"


def _load_scores(path: Path) -> list[CaseScore]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [CaseScore(**row) for row in payload["cases"]]


def _print_plan() -> None:
    print(
        "Overnight loop (one change, then the gate, then revert on a miss)\n"
        "\n"
        "  1. Freeze a baseline:\n"
        "       python -m eval.run --suite fast\n"
        "  2. See which knobs move regret:\n"
        "       python -m eval.sensitivity --suite fast --baseline <results.json>\n"
        "  3. Trial overrides, accepted only by the select-set gate:\n"
        "       python -m eval.improve --baseline <results.json> --moves moves.json --suite fast\n"
        "  4. Structural edits (heading policy, prune) are done in router.py between\n"
        "     steps, then re-scored with eval.run. This driver does not rewrite source.\n"
        "  5. After the last accepted change, run confirm races and write the report:\n"
        "       python -m eval.run --suite confirm\n"
        "       python -m eval.report --run <results.json> --out docs/routing-eval-report.md\n"
        "\n"
        "A trial is accepted only when every tune and select case is feasible, select\n"
        "median regret improves by at least 0.5 points (or worst-case by 1 point\n"
        "without the median getting more than 0.2 points worse), no select case gets\n"
        "more than 2 points worse, and fast-suite p95 route time stays within 1.5x.\n"
        "Confirm races are not used to accept a change.\n"
        f"Frozen knob: {', '.join(sorted(FROZEN_PARAMS))}."
    )


def run_moves(
    baseline_path: Path,
    moves: list[dict[str, Any]],
    *,
    suite: str,
    ids: list[str] | None,
) -> Path:
    baseline = _load_scores(baseline_path)
    cases = select_cases(suite, ids)
    wanted = {c.id for c in cases}
    baseline = [s for s in baseline if s.id in wanted]
    if not baseline:
        raise SystemExit(f"baseline {baseline_path} has none of the requested cases")
    boat = load_boat()
    kept: dict[str, Any] = {}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    for i, move in enumerate(moves, start=1):
        frozen = [k for k in move if k in FROZEN_PARAMS]
        if frozen:
            print(f"trial {i}: skip frozen {frozen}")
            continue
        trial_params = {**kept, **move}
        print(f"trial {i}: {trial_params}", flush=True)
        scores = execute(cases, boat, product_params(**trial_params))
        decision = decide(baseline, scores)
        row = {
            "utc": datetime.now(UTC).isoformat(),
            "trial": i,
            "move": move,
            "params": trial_params,
            "accepted": decision.accepted,
            "reasons": decision.reasons,
            "median_base": decision.median_base,
            "median_trial": decision.median_trial,
            "worst_base": decision.worst_base,
            "worst_trial": decision.worst_trial,
        }
        with JOURNAL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        if decision.accepted:
            kept.update(move)
            baseline = scores
            print(f"  accepted. median {decision.median_base} -> {decision.median_trial}")
        else:
            print("  rejected: " + "; ".join(decision.reasons))
    out = baseline_path.parent / "best_overrides.json"
    out.write_text(json.dumps(kept, indent=2), encoding="utf-8")
    print(f"kept overrides: {kept or '{}'}")
    print(f"wrote {out}")
    return out


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("app").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Accept or reject router parameter moves")
    parser.add_argument("--dry-run", action="store_true", help="Print the loop and exit")
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--moves", type=Path, default=None)
    parser.add_argument("--suite", default="fast")
    parser.add_argument("--ids", default="")
    args = parser.parse_args()
    if args.dry_run:
        _print_plan()
        return 0
    if args.baseline is None or args.moves is None:
        raise SystemExit("pass --baseline and --moves, or --dry-run")
    moves = json.loads(args.moves.read_text(encoding="utf-8"))
    if not isinstance(moves, list):
        raise SystemExit("moves file must be a JSON list of objects")
    ids = [p.strip() for p in args.ids.split(",") if p.strip()] or None
    run_moves(args.baseline, moves, suite=args.suite, ids=ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
