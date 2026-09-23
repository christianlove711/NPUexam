"""Audit the 36 historical regression scenes against available v7 runs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from official import canonical_hash, read_json, score
from run import job_paths


def checked_result(run, case, cores, scene):
    job_path, plan_path = job_paths(Path(run), case, cores, scene)
    if not job_path.is_file() or not plan_path.is_file():
        return None
    job, plan = read_json(job_path), read_json(plan_path)
    if job.get("status") != "ok":
        return None
    expected = job.get("selected_plan_hash")
    if expected and expected != canonical_hash(plan):
        raise ValueError(f"plan hash mismatch: {plan_path}")
    return score(job["selected"])


def audit(targets, prior_runs, new_runs):
    rows = []
    for target in targets:
        args = target["case"], target["cores"], target["scene"]
        prior = [(name, result) for name, run in prior_runs.items()
                 if (result := checked_result(run, *args)) is not None]
        current = [(str(run), result) for run in new_runs
                   if (result := checked_result(run, *args)) is not None]
        previous = min(prior, key=lambda item: item[1]) if prior else None
        best = min(current, key=lambda item: item[1]) if current else None
        rows.append({"case": args[0], "cores": args[1], "scene": args[2],
                     "previous_source": previous[0] if previous else None,
                     "previous_score": previous[1] if previous else None,
                     "new_run": best[0] if best else None,
                     "new_score": best[1] if best else None,
                     "status": ("missing_previous" if previous is None else
                                "missing_v7" if best is None else
                                "regression" if best[1] > previous[1] else
                                "improved" if best[1] < previous[1] else "tied")})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, default=Path(__file__).with_name("regression_targets.json"))
    parser.add_argument("--v4-run", type=Path, required=True)
    parser.add_argument("--v5-run", type=Path)
    parser.add_argument("--astra-run", type=Path)
    parser.add_argument("--v6-run", type=Path)
    parser.add_argument("--runs", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true", help="fail while any target remains untested")
    args = parser.parse_args()
    sources = {name: run for name in ("v4", "v5", "astra", "v6")
               if (run := getattr(args, f"{name}_run")) is not None}
    rows = audit(read_json(args.targets)["targets"], sources, args.runs)
    counts = {kind: sum(row["status"] == kind for row in rows)
              for kind in ("improved", "tied", "regression", "missing_v7", "missing_previous")}
    print(json.dumps(counts, ensure_ascii=False))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(bool(counts["regression"] or counts["missing_previous"] or
                    (args.strict and counts["missing_v7"])))


if __name__ == "__main__":
    raise SystemExit(main())
