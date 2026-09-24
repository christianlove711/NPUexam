"""Refine problem-one plans against the official evaluator.

This experiment reads saved v4/v5 plans as incumbents, re-evaluates them, and
only writes separate experiment results.  It never edits historical runs.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from official import DATA, PROJECT, atomic_json, canonical_hash, evaluate, read_json, score, settings
from solver import neighbors

V4 = PROJECT / "多核调度_第四版" / "runs" / "20260923_141723_v4"
V5 = PROJECT / "npu_5" / "runs" / "20260923_220123_13c740"


def saved_plan(run, case, cores):
    path = run / "problem_1" / "plans" / f"{case}_{cores}core.json"
    return read_json(path) if path.is_file() else None


def refine(graph, cores, seeds, budget):
    config = settings()
    seen = set()
    records = []
    best = None

    def trial(label, plan):
        nonlocal best
        digest = canonical_hash(plan)
        if digest in seen or len(records) >= budget:
            return False
        seen.add(digest)
        try:
            metric = evaluate(graph, plan, "A", config)
            record = {"label": label, "status": "ok", **metric}
            if best is None or score(metric) < score(best[2]):
                best = label, plan, metric
                improved = True
            else:
                improved = False
        except Exception as exc:
            record = {"label": label, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
            improved = False
        records.append(record)
        return improved

    for label, plan in seeds:
        if plan is not None:
            trial(label, plan)
    if best is None:
        raise RuntimeError("no valid initial plan")
    seed_best = best[2]["makespan"]
    # Merges remove one Task boundary. Whole-Task moves change core balance.
    # Evaluate those before further splits, which often increase DDR traffic.
    for round_id in range(3):
        if len(records) >= budget:
            break
        current = best[1]
        pool = list(neighbors(graph, current, "A", limit=10000))
        priority = {"merge": 0, "move": 1, "swap": 2, "split": 3}
        pool.sort(key=lambda item: (priority.get(item[0].split("_", 1)[0], 4), item[0]))
        changed = False
        for label, plan in pool:
            if len(records) >= budget:
                break
            changed |= trial(f"round{round_id + 1}_{label}", plan)
        if not changed:
            break
    return best, records, seed_best


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="+", default=["case_007", "case_022", "case_078", "case_093"])
    parser.add_argument("--cores", type=int, default=5, choices=(2, 3, 4, 5))
    parser.add_argument("--budget", type=int, default=24)
    parser.add_argument("--output", type=Path, default=PROJECT / "npu_5" / "a_refinement")
    args = parser.parse_args()
    if args.budget < 2:
        parser.error("budget must be at least 2")
    rows = []
    for case in args.cases:
        graph = read_json(DATA / f"{case}.json")
        seeds = [("v4", saved_plan(V4, case, args.cores)),
                 ("v5", saved_plan(V5, case, args.cores))]
        best, records, seed_best = refine(graph, args.cores, seeds, args.budget)
        name = f"{case}_{args.cores}core"
        atomic_json(args.output / "plans" / f"{name}.json", best[1])
        atomic_json(args.output / "jobs" / f"{name}.json",
                    {"case": case, "cores": args.cores, "selected_source": best[0],
                     "selected": best[2], "seed_best_makespan": seed_best,
                     "evaluations": records})
        row = {"case": case, "cores": args.cores, "seed_best": seed_best,
               "makespan": best[2]["makespan"], "gain_pct": round(100 * (seed_best / best[2]["makespan"] - 1), 4),
               "source": best[0], "evaluations": len(records)}
        rows.append(row)
        print(json.dumps(row), flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
