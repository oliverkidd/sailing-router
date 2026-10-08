"""Write the human-readable eval report from a results JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _fmt_regret(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.1f}%"


def _fmt_s(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 3600:
        return f"{value / 3600:.2f} h"
    if value >= 120:
        return f"{value / 60:.1f} min"
    return f"{value:.0f} s"


def render(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Routing eval")
    lines.append("")
    lines.append(f"Suite: `{payload.get('suite', '')}`  ")
    lines.append(f"When: {payload.get('created_utc', '')}  ")
    if payload.get("git"):
        lines.append(f"Git: `{payload['git']}`  ")
    lines.append("")
    oracle = payload.get("oracle_check") or {}
    if oracle:
        lines.append("## Oracle check")
        lines.append("")
        lines.append(f"Trusted: {'yes' if oracle.get('ok') else 'no'}")
        lines.append("")
        for row in oracle.get("cases", []):
            lines.append(
                f"- `{row['id']}` exact {_fmt_s(row.get('exact_s'))}, "
                f"A* {_fmt_s(row.get('astar_s'))}, "
                f"gap {row.get('astar_rel', 0) * 100:+.2f}%"
            )
        lines.append("")

    lines.append("## Cases")
    lines.append("")
    lines.append("| Case | Split | Feasible | Regret | Router | Reference | Route time | Tag |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in payload.get("cases", []):
        feas = "yes" if row.get("feasible") else "no"
        if row.get("status") != "ok":
            feas = "error"
        lines.append(
            "| `{id}` | {split} | {feas} | {regret} | {router} | {ref} | {runtime} | {tag} |".format(
                id=row["id"],
                split=row.get("split", ""),
                feas=feas,
                regret=_fmt_regret(row.get("regret")),
                router=_fmt_s(row.get("router_elapsed_s")),
                ref=_fmt_s(row.get("ref_s")),
                runtime=f"{row.get('router_runtime_s', 0):.2f} s",
                tag=row.get("tag", ""),
            )
        )
    lines.append("")
    lines.append("")
    external = payload.get("external") or []
    if external:
        lines.append("## libweatherrouting")
        lines.append("")
        lines.append("Same wind and the same polar. A positive gap means our router is slower.")
        lines.append("")
        lines.append("| Case | Their time | Feasible | Ours vs theirs | Theirs vs proved |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row in external:
            gap = row.get("our_minus_theirs_rel")
            proved = row.get("theirs_minus_proved_rel")
            lines.append(
                "| `{id}` | {elapsed} | {feas} | {gap} | {proved} |".format(
                    id=row["id"],
                    elapsed=_fmt_s(row.get("elapsed_s")),
                    feas="yes" if row.get("feasible") else "no",
                    gap=_fmt_regret(gap),
                    proved=_fmt_regret(proved),
                )
            )
        lines.append("")
    summary = payload.get("summary") or {}
    if summary:
        lines.append("## By split")
        lines.append("")
        for split, stats in summary.items():
            lines.append(
                f"- **{split}**: median regret {_fmt_regret(stats.get('median_regret'))}, "
                f"worst {_fmt_regret(stats.get('worst_regret'))}, "
                f"{stats.get('feasible', 0)}/{stats.get('n', 0)} feasible"
            )
        lines.append("")
    lines.append(
        "Regret is `(router time − reference time) / reference time` on the same "
        "boat and the same wind. Negative would mean the router beat the reference. "
        "Chart races without a cached oracle are feasibility and route-time only."
    )
    lines.append("")
    return "\n".join(lines)


def write_report(payload: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(payload), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a routing-eval report")
    parser.add_argument("--run", type=Path, required=True, help="results.json from eval.run")
    parser.add_argument("--out", type=Path, default=None, help="Markdown path (default: beside the JSON)")
    args = parser.parse_args()
    payload = json.loads(args.run.read_text(encoding="utf-8"))
    out = args.out or args.run.with_suffix(".md")
    write_report(payload, out)
    print(out)


if __name__ == "__main__":
    main()
