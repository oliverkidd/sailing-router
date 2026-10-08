"""Accept or reject a trial against a frozen baseline.

Thresholds match the eval plan:

- every tune and select case on the trial must be feasible
- select median regret improves by >= 0.5 percentage points, or worst-case
  regret improves by >= 1 point while median regret is not worse by more than
  0.2 points
- no individual select case gets more than 2 points worse
- fast-suite p95 router time stays within 1.5x the baseline
- a case the baseline missed and the trial finishes is a feasibility win, as
  long as the overlapping regret does not get materially worse

Confirm cases are not consulted. ``depth_safety_margin_m`` is not a speed knob.
"""

from __future__ import annotations

import math
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from eval.score import CaseScore

MEDIAN_GAIN = 0.005
WORST_GAIN = 0.01
MEDIAN_SLACK = 0.002
CASE_REGRESS = 0.02
RUNTIME_FACTOR = 1.5

API_ROOT = Path(__file__).resolve().parents[1]
LEGACY_SCRIPT = API_ROOT / "scripts" / "validate_routing.py"


@dataclass
class GateDecision:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    median_base: float | None = None
    median_trial: float | None = None
    worst_base: float | None = None
    worst_trial: float | None = None
    runtime_base_s: float | None = None
    runtime_trial_s: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "reasons": self.reasons,
            "median_base": self.median_base,
            "median_trial": self.median_trial,
            "worst_base": self.worst_base,
            "worst_trial": self.worst_trial,
            "runtime_base_s": self.runtime_base_s,
            "runtime_trial_s": self.runtime_trial_s,
        }


def _by_id(scores: list[CaseScore]) -> dict[str, CaseScore]:
    return {s.id: s for s in scores}


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * 0.95
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] * (hi - k) + ordered[hi] * (k - lo)


def decide(baseline: list[CaseScore], trial: list[CaseScore]) -> GateDecision:
    """Compare ``trial`` to ``baseline``. Both lists must cover the same case ids."""
    base = _by_id(baseline)
    cur = _by_id(trial)
    reasons: list[str] = []
    missing = sorted(set(base) - set(cur))
    if missing:
        reasons.append("trial is missing cases: " + ", ".join(missing))

    for split in ("tune", "select"):
        for case_id, row in cur.items():
            if row.split != split:
                continue
            if row.status != "ok":
                reasons.append(f"{case_id} did not run ({row.error or row.status})")
                continue
            if not row.feasible:
                reasons.append(f"{case_id} is infeasible ({', '.join(row.fails) or 'unknown'})")

    base_sel_ids = [i for i, row in base.items() if row.split == "select" and i in cur]
    regressions: list[str] = []
    for case_id in base_sel_ids:
        b, t = base[case_id], cur[case_id]
        if b.regret is None or t.regret is None:
            continue
        if t.regret - b.regret > CASE_REGRESS:
            regressions.append(f"{case_id} regret {b.regret:+.3f} -> {t.regret:+.3f}")
    if regressions:
        reasons.append("select case regression: " + "; ".join(regressions))

    overlap = [
        i
        for i in base_sel_ids
        if base[i].regret is not None and cur[i].regret is not None and cur[i].feasible
    ]
    median_base = median_trial = worst_base = worst_trial = None
    improved = False
    if overlap:
        b_regs = [float(base[i].regret) for i in overlap if base[i].regret is not None]
        t_regs = [float(cur[i].regret) for i in overlap if cur[i].regret is not None]
        median_base = _median(b_regs)
        median_trial = _median(t_regs)
        worst_base = max(b_regs)
        worst_trial = max(t_regs)
        d_med = median_base - median_trial
        d_worst = worst_base - worst_trial
        improved = d_med >= MEDIAN_GAIN or (d_worst >= WORST_GAIN and d_med >= -MEDIAN_SLACK)

    tune_ids = [i for i, row in base.items() if row.split == "tune"]
    newly = [
        i
        for i in base_sel_ids + tune_ids
        if i in cur and (not base[i].feasible) and cur[i].feasible and cur[i].status == "ok"
    ]
    feasibility_win = False
    if newly and overlap and median_base is not None and median_trial is not None:
        feasibility_win = median_trial - median_base <= MEDIAN_SLACK
    elif newly and not overlap:
        feasibility_win = True

    base_rt = [s.router_runtime_s for s in baseline if s.fast and s.status == "ok"]
    trial_rt = [
        cur[s.id].router_runtime_s
        for s in baseline
        if s.fast and s.status == "ok" and s.id in cur and cur[s.id].status == "ok"
    ]
    runtime_base = _p95(base_rt) if base_rt else None
    runtime_trial = _p95(trial_rt) if trial_rt else None
    if (
        runtime_base is not None
        and runtime_trial is not None
        and runtime_base > 0
        and runtime_trial > RUNTIME_FACTOR * runtime_base
    ):
        reasons.append(
            f"fast-suite p95 router time {runtime_trial:.2f}s exceeds "
            f"{RUNTIME_FACTOR:.1f}x baseline {runtime_base:.2f}s"
        )

    if not any(row.split == "select" for row in baseline):
        reasons.append("baseline has no select cases")
    elif not improved and not feasibility_win:
        if overlap and median_base is not None and median_trial is not None:
            reasons.append(
                "no select improvement "
                f"(median {median_base:.4f} -> {median_trial:.4f}, "
                f"worst {worst_base:.4f} -> {worst_trial:.4f})"
            )
        elif not newly:
            reasons.append("no select reference scores to compare")

    accepted = (improved or feasibility_win) and not reasons
    return GateDecision(
        accepted=accepted,
        reasons=reasons,
        median_base=median_base,
        median_trial=median_trial,
        worst_base=worst_base,
        worst_trial=worst_trial,
        runtime_base_s=runtime_base,
        runtime_trial_s=runtime_trial,
    )


def run_legacy_validate(*, acceptance: bool = True) -> tuple[int, str]:
    """The existing Solent smoke gates. Not part of the regret score."""
    cmd = [sys.executable, str(LEGACY_SCRIPT), "--sample", "--no-baseline"]
    if acceptance:
        cmd.append("--acceptance")
    proc = subprocess.run(cmd, cwd=API_ROOT, capture_output=True, text=True, check=False)
    tail = (proc.stdout + "\n" + proc.stderr).strip()
    if len(tail) > 4000:
        tail = tail[-4000:]
    return proc.returncode, tail


def self_check() -> None:
    """Gate decisions on hand-built scores. No router."""

    def row(
        case_id: str,
        split: str,
        regret: float | None,
        *,
        feasible: bool = True,
        runtime: float = 1.0,
        fast: bool = True,
    ) -> CaseScore:
        return CaseScore(
            id=case_id,
            split=split,
            fast=fast,
            description="",
            status="ok",
            feasible=feasible,
            fails=[] if feasible else ["unreachable"],
            regret=regret,
            router_elapsed_s=100.0,
            ref_s=None if regret is None else 100.0,
            router_runtime_s=runtime,
            oracle_runtime_s=0.0,
            n_tacks=1,
            n_gybes=0,
            ref_tacks=1,
            ref_gybes=0,
            tag="ok" if feasible else "unreachable",
            method="exact",
        )

    base = [
        row("t1", "tune", 0.05),
        row("s1", "select", 0.10),
        row("s2", "select", 0.12),
    ]
    better = [
        row("t1", "tune", 0.05),
        row("s1", "select", 0.09),
        row("s2", "select", 0.11),
    ]
    decision = decide(base, better)
    if not decision.accepted:
        raise AssertionError(f"expected accept, got {decision.reasons}")

    one_case_worse = [
        row("t1", "tune", 0.05),
        row("s1", "select", 0.07),
        row("s2", "select", 0.15),
    ]
    decision = decide(base, one_case_worse)
    if decision.accepted or not any("regression" in r for r in decision.reasons):
        raise AssertionError(f"a 3-point select regression must be rejected: {decision.reasons}")

    # Median improves, but one held-out case gives the gain back.
    mixed = [
        row("t1", "tune", 0.05),
        row("s1", "select", 0.04),
        row("s2", "select", 0.15),
    ]
    decision = decide(base, mixed)
    if decision.accepted:
        raise AssertionError("median gain must not hide a select regression")

    slow = [
        row("t1", "tune", 0.04, runtime=4.0),
        row("s1", "select", 0.08, runtime=4.0),
        row("s2", "select", 0.08, runtime=4.0),
    ]
    decision = decide(base, slow)
    if decision.accepted:
        raise AssertionError("a 4x runtime must be rejected")

    broken = [
        row("t1", "tune", 0.01),
        row("s1", "select", None, feasible=False),
        row("s2", "select", 0.01),
    ]
    decision = decide(base, broken)
    if decision.accepted:
        raise AssertionError("an infeasible select case must be rejected")

    base_miss = [
        row("t1", "tune", 0.05),
        row("s1", "select", None, feasible=False),
        row("s2", "select", 0.10),
    ]
    recovered = [
        row("t1", "tune", 0.05),
        row("s1", "select", 0.04),
        row("s2", "select", 0.10),
    ]
    decision = decide(base_miss, recovered)
    if not decision.accepted:
        raise AssertionError(f"finishing a case the baseline missed must be accepted: {decision.reasons}")
