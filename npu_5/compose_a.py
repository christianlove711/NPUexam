"""Select the best officially measured problem-one plan from v4, v5, and refinements."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from official import DATA, PROJECT, atomic_json, canonical_hash, evaluate, read_json, score, settings

DEFAULT_V4 = PROJECT / "多核调度_第四版" / "runs" / "20260923_141723_v4"
DEFAULT_V5 = PROJECT / "npu_5" / "runs" / "20260923_220123_13c740"


def entry(run, case, cores):
    stem = f"{case}_{cores}core.json"
    job_path = run / "problem_1" / "jobs" / stem
    plan_path = run / "problem_1" / "plans" / stem
    if not job_path.is_file() or not plan_path.is_file():
        return None
    job, plan = read_json(job_path), read_json(plan_path)
    if job.get("status") != "ok":
        raise ValueError(f"non-ok job: {job_path}")
    if job.get("selected_plan_hash") not in (None, canonical_hash(plan)):
        raise ValueError(f"plan hash mismatch: {plan_path}")
    return plan, job["selected"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v4", type=Path, default=DEFAULT_V4)
    parser.add_argument("--v5", type=Path, default=DEFAULT_V5)
    parser.add_argument("--refined", type=Path, action="append",
                        help="refinement directory; may be repeated")
    parser.add_argument("--output", type=Path, default=PROJECT / "npu_5" / "a_optimized")
    parser.add_argument("--verify", action="store_true", help="replay every selected plan in the official evaluator")
    args = parser.parse_args()
    refined_dirs = args.refined or [PROJECT / "npu_5" / "a_refinement",
                                    PROJECT / "npu_5" / "a_refinement_highspeed"]
    if args.output.resolve() in {path.resolve() for path in (args.v4, args.v5, *refined_dirs)}:
        parser.error("output must be separate from input runs")
    for source in (args.v4, args.v5):
        validation = read_json(source / "reports" / "validation.json")
        if validation["status"] != "ok":
            raise ValueError(f"source run has not passed validation: {source}")
    rows = []
    config = settings() if args.verify else None
    for number in range(1, 101):
        case = f"case_{number:03d}"
        for cores in (2, 3, 4, 5):
            candidates = []
            for label, source in (("v4", args.v4), ("v5", args.v5)):
                item = entry(source, case, cores)
                if item is None:
                    raise FileNotFoundError(f"missing {label} result: {case} {cores}")
                candidates.append((score(item[1]), label, *item))
            for refined_dir in refined_dirs:
                refined_path = refined_dir / "jobs" / f"{case}_{cores}core.json"
                if refined_path.is_file():
                    refined_job = read_json(refined_path)
                    refined_plan = read_json(refined_dir / "plans" / f"{case}_{cores}core.json")
                    if refined_job["case"] != case or refined_job["cores"] != cores:
                        raise ValueError(f"refinement identity mismatch: {refined_path}")
                    candidates.append((score(refined_job["selected"]), "refined",
                                       refined_plan, refined_job["selected"]))
            _, label, plan, metrics = min(candidates, key=lambda item: (item[0], item[1]))
            if args.verify:
                replay = evaluate(read_json(DATA / f"{case}.json"), plan, "A", config)
                if score(replay) != score(metrics):
                    raise ValueError(f"official replay mismatch: {case} {cores} {label}")
            baseline = read_json(args.v5 / "baseline" / "jobs" / f"{case}.json")["makespan"]
            path = args.output / "plans" / f"{case}_{cores}core.json"
            atomic_json(path, plan)
            rows.append({"case": case, "cores": cores, "source": label,
                         "singlecore_makespan": baseline, "makespan": metrics["makespan"],
                         "speedup": baseline / metrics["makespan"],
                         "added_copy_bytes": metrics["added_copy_bytes"]})
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    for cores in (2, 3, 4, 5):
        subset = [row for row in rows if row["cores"] == cores]
        mean = sum(row["speedup"] for row in subset) / len(subset)
        print(f"{cores} cores: mean speedup {mean:.6f}; "
              f"v4={sum(row['source']=='v4' for row in subset)}, "
              f"v5={sum(row['source']=='v5' for row in subset)}, "
              f"refined={sum(row['source']=='refined' for row in subset)}")
    print(f"output={args.output.resolve()}")


if __name__ == "__main__":
    main()
