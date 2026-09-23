"""Check a saved npu_5 run and replay representative plans officially."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from run import ATTACHMENT, FOLDERS, V4_RUN, validate
from solver import derive_multicore_plan
from worker import official_evaluate, read_json, score, settings


def representative_replay(run):
    manifest = read_json(run / "manifest.json")
    config = settings()
    chosen = set()
    for scene, folder in FOLDERS.items():
        for core in manifest["cores"]:
            rows = []
            for case in manifest["cases"]:
                stem = f"{case}_{core}core.json"
                new = read_json(run / folder / "jobs" / stem)["selected"]
                old = read_json(V4_RUN / folder / "jobs" / stem)["selected"]
                rows.append((old["makespan"] / new["makespan"], case))
            chosen.add((scene, max(rows)[1], core))
            unchanged = next((case for ratio, case in rows if ratio == 1), None)
            if unchanged is not None:
                chosen.add((scene, unchanged, core))
    results = []
    issues = []
    for scene, case, core in sorted(chosen):
        folder = FOLDERS[scene]
        stem = f"{case}_{core}core.json"
        graph = read_json(ATTACHMENT / "data" / f"{case}.json")
        plan = read_json(run / folder / "plans" / stem)
        expected = read_json(run / folder / "jobs" / stem)["selected"]
        try:
            derive_multicore_plan(graph, plan)
            actual = official_evaluate(graph, plan, scene, config)
            ok = score(actual) == score(expected)
            if not ok:
                issues.append(f"replay mismatch {scene} {case} {core}")
            results.append({"scene": scene, "case": case, "cores": core,
                            "ok": ok, "makespan": actual["makespan"]})
        except Exception as exc:
            issues.append(f"replay error {scene} {case} {core}: {exc}")
        if scene == "L2":
            try:
                b_plan = read_json(run / "problem_2" / "plans" / stem)
                paired = read_json(run / folder / "jobs" / stem)["paired"]
                actual_pair = official_evaluate(graph, b_plan, "L2", config)
                if score(actual_pair) != score(paired["with_l2"]):
                    issues.append(f"paired L2 replay mismatch {case} {core}")
            except Exception as exc:
                issues.append(f"paired L2 replay error {case} {core}: {exc}")
    return results, issues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--replay", action="store_true",
                        help="re-evaluate best-gain and unchanged representatives")
    args = parser.parse_args(argv)
    run = args.run.resolve()
    audit = validate(run)
    replayed, replay_issues = representative_replay(run) if args.replay and audit["status"] == "ok" else ([], [])
    audit["replayed"] = replayed
    audit["issues"].extend(replay_issues)
    audit["status"] = "ok" if not audit["issues"] else "error"
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0 if audit["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
