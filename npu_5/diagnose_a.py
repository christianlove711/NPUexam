"""Summarize existing problem-one jobs without running the official simulator."""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

from official import DATA, read_json
from graph import graph_view


def component_count(graph):
    _, order, preds = graph_view(graph)
    parent = {node: node for node in order}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for dst, links in preds.items():
        for src in links:
            parent[find(src)] = find(dst)
    return len({find(node) for node in order})


def diagnose(run: Path):
    rows = []
    for job_path in sorted((run / "problem_1" / "jobs").glob("case_*_5core.json")):
        job = read_json(job_path)
        case = job["case"]
        plan = read_json(run / "problem_1" / "plans" / job_path.name)
        graph = read_json(DATA / f"{case}.json")
        baseline = read_json(run / "baseline" / "jobs" / f"{case}.json")
        selected = job["selected"]
        evaluations = job.get("evaluations", [])
        valid = [entry for entry in evaluations if "makespan" in entry]
        reasons = Counter(entry["error"].split(":", 1)[0]
                          for entry in job.get("errors", []))
        op_count = sum(op["op"] not in {"COPY_IN", "COPY_OUT"}
                       for op in graph["ops"])
        components = component_count(graph)
        tasks = sum(map(len, plan["core_schedules"]))
        active = sum(bool(schedule) for schedule in plan["core_schedules"])
        floor = selected["scheduled_copy_bytes"] / 60 / selected["makespan"]
        if active == 1 and components > 1:
            bottleneck = "independent_components_missed"
        elif active == 1:
            bottleneck = "single_active_core_or_cut"
        elif floor >= 0.8:
            bottleneck = "selected_plan_ddr"
        elif tasks >= op_count // 2:
            bottleneck = "fine_task_search"
        else:
            bottleneck = "parallelism_or_schedule"
        rows.append({
            "case": case,
            "ops": op_count,
            "independent_components": components,
            "speedup": baseline["makespan"] / selected["makespan"],
            "selected_makespan": selected["makespan"],
            "active_cores": active,
            "tasks": tasks,
            "added_copy_bytes": selected["added_copy_bytes"],
            "selected_plan_ddr_floor_fraction": round(floor, 4),
            "job_seconds": job["total_seconds"],
            "official_calls": job["official_evaluations"],
            "valid_candidates": len(valid),
            "invalid_candidates": len(job.get("errors", [])),
            "invalid_reasons": "; ".join(
                f"{reason}={count}" for reason, count in sorted(reasons.items())),
            "candidate_evaluation_seconds": round(sum(
                entry.get("evaluation_seconds", 0) for entry in valid), 3),
            "fixed_1_seconds": round(sum(entry.get("evaluation_seconds", 0)
                                         for entry in valid if entry["label"] == "fixed_1"), 3),
            "bottleneck": bottleneck,
            "selected_source": job["selected_source"],
        })
    return rows


def candidate_rows(run: Path):
    """Keep every historical candidate result and failure for later inspection."""
    rows = []
    for path in sorted((run / "problem_1" / "jobs").glob("case_*_5core.json")):
        job = read_json(path)
        for entry in job.get("evaluations", []):
            rows.append({"case": job["case"], "label": entry["label"],
                         "status": "ok", "makespan": entry.get("makespan", ""),
                         "added_copy_bytes": entry.get("added_copy_bytes", ""),
                         "evaluation_seconds": entry.get("evaluation_seconds", ""),
                         "error": ""})
        for entry in job.get("errors", []):
            rows.append({"case": job["case"], "label": entry["label"],
                         "status": "error", "makespan": "",
                         "added_copy_bytes": "", "evaluation_seconds": "",
                         "error": entry["error"]})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = diagnose(args.run)
    if not rows:
        parser.error("no five-core problem-one jobs in run")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    candidates = candidate_rows(args.run)
    candidates_path = args.output.with_name(args.output.stem + "_candidates.csv")
    with candidates_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(candidates[0]))
        writer.writeheader()
        writer.writerows(candidates)
    print(f"cases={len(rows)} categories={dict(Counter(x['bottleneck'] for x in rows))}")
    print(f"diagnostics={args.output.resolve()}")
    print(f"candidates={candidates_path.resolve()}")


if __name__ == "__main__":
    main()
