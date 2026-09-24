"""Check fifth-version run completeness and optionally replay representative plans."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from official import DATA, read_json, settings, evaluate, score
from run import FOLDERS, job_paths, validate


def representative_replay(run):
    manifest = read_json(run / "manifest.json")
    config = settings()
    issues, replayed = [], []
    cases = manifest["cases"]
    picks = list(dict.fromkeys((cases[0], cases[len(cases) // 2], cases[-1])))
    for case in picks:
        graph = read_json(DATA / f"{case}.json")
        for core in manifest["cores"]:
            for scene in manifest.get("scenes", list(FOLDERS)):
                job_path, plan_path = job_paths(run, case, core, scene)
                job, plan = read_json(job_path), read_json(plan_path)
                actual = evaluate(graph, plan, scene, config)
                ok = score(actual) == score(job["selected"])
                replayed.append({"case": case, "cores": core, "scene": scene, "ok": ok})
                if not ok:
                    issues.append(f"replay mismatch {scene} {case} {core}")
                if scene == "L2":
                    b_plan = read_json(job_paths(run, case, core, "B")[1])
                    paired = evaluate(graph, b_plan, "L2", config)
                    if score(paired) != score(job["paired"]["with_l2"]):
                        issues.append(f"paired replay mismatch {case} {core}")
    return replayed, issues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args(argv)
    run = args.run.resolve()
    audit = validate(run)
    if args.replay and audit["status"] == "ok":
        replayed, issues = representative_replay(run)
        audit["representative_replays"] = replayed
        audit["issues"].extend(issues)
        audit["status"] = "ok" if not audit["issues"] else "error"
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0 if audit["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
