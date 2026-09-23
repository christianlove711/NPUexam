"""Independent fifth-version official search and resumable batch runner."""
from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from official import (ROOT, CODE, DATA, atomic_json, read_json, file_hash,
                      canonical_hash, settings, evaluate, baseline, score, attempt,
                      derive_multicore_plan)
from solver import initial_candidates, neighbors

FOLDERS = {"A": "problem_1", "B": "problem_2", "L2": "problem_3"}


def source_hashes():
    return {str(path.relative_to(ROOT.parent)): file_hash(path)
            for directory in (ROOT, CODE) for path in sorted(directory.glob("*.py"))}


def input_hashes(cases):
    return {case: file_hash(DATA / f"{case}.json") for case in cases}


def job_paths(run, case, cores, scene):
    stem = f"{case}_{cores}core.json"
    folder = run / FOLDERS[scene]
    return folder / "jobs" / stem, folder / "plans" / stem


def complete_job(run, case, cores, scene):
    try:
        job_path, plan_path = job_paths(run, case, cores, scene)
        job, plan = read_json(job_path), read_json(plan_path)
        if job["status"] != "ok" or job["selected_plan_hash"] != canonical_hash(plan):
            return False
        if scene == "L2":
            b_job, b_plan = job_paths(run, case, cores, "B")
            return (job["b_plan_hash"] == canonical_hash(read_json(b_plan))
                    and job["paired"]["no_l2"]["makespan"] == read_json(b_job)["selected"]["makespan"])
        return True
    except (FileNotFoundError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return False


def search(graph, cores, scene, config, budget, extras=(), preevaluated=()):
    evaluations, errors, leaders, seen = [], [], [], set()
    calls = 0

    def add(label, plan, metrics=None):
        nonlocal calls
        digest = canonical_hash(plan)
        if digest in seen:
            return
        seen.add(digest)
        if metrics is None:
            if calls >= budget:
                return
            calls += 1
            metrics = attempt(graph, plan, scene, config, label, evaluations, errors)
        else:
            evaluations.append({"label": label, **metrics})
        if metrics:
            leaders.append((score(metrics), label, plan, metrics))
            leaders.sort(key=lambda item: (item[0], item[1]))
            del leaders[4:]

    for label, plan, metrics in preevaluated:
        add(label, plan, metrics)
    for label, plan in extras:
        add(label, plan)
    try:
        for label, plan in initial_candidates(graph, cores, scene, config):
            if calls >= budget:
                break
            add(label, plan)
    except Exception as exc:
        errors.append({"label": "initial_generation", "error": f"{type(exc).__name__}: {exc}"})
    if not leaders:
        raise RuntimeError(f"No official-valid {scene} plan: {errors[:10]}")
    starts = leaders[:2]
    for _, label, plan, _ in starts:
        current = plan
        for round_id in range(2):
            if calls >= budget:
                break
            before = leaders[0][0]
            try:
                for move_label, candidate in neighbors(graph, current, scene, limit=48):
                    if calls >= budget:
                        break
                    add(f"local{round_id + 1}_{label}_{move_label}", candidate)
            except Exception as exc:
                errors.append({"label": f"local_{label}", "error": f"{type(exc).__name__}: {exc}"})
            if leaders[0][0] >= before:
                break
            current = leaders[0][2]
    return leaders[0], leaders, evaluations, errors, calls


def optimize(run, case, cores, small_budget, large_budget):
    run = Path(run)
    if all(complete_job(run, case, cores, scene) for scene in FOLDERS):
        return "cached"
    graph = read_json(DATA / f"{case}.json")
    config = settings()
    count = sum(op["op"] not in {"COPY_IN", "COPY_OUT"} for op in graph["ops"])
    budget = small_budget if count <= 5000 else large_budget
    winners, leaders_by_scene = {}, {}
    for scene in FOLDERS:
        if complete_job(run, case, cores, scene):
            job_path, plan_path = job_paths(run, case, cores, scene)
            job, plan = read_json(job_path), read_json(plan_path)
            winners[scene] = (score(job["selected"]), job["selected_source"], plan, job["selected"])
            leaders_by_scene[scene] = [winners[scene]]
            continue
        started = time.perf_counter()
        extras, preevaluated = [], []
        paired = None
        b_hash = None
        if scene == "B":
            extras.append(("a_selected", winners["A"][2]))
        if scene == "L2":
            b_plan, b_metrics = winners["B"][2], winners["B"][3]
            b_hash = canonical_hash(b_plan)
            cache = evaluate(graph, b_plan, "L2", config)
            paired = {"no_l2": b_metrics, "with_l2": cache,
                      "cache_speedup": b_metrics["makespan"] / cache["makespan"]}
            preevaluated.append(("paired_b_plan", b_plan, cache))
            extras.append(("a_selected", winners["A"][2]))
            extras.extend((f"b_candidate_{label}", plan)
                          for _, label, plan, _ in leaders_by_scene["B"][:4])
        best, leaders, evaluations, errors, calls = search(
            graph, cores, scene, config, budget, extras, preevaluated)
        if scene == "L2":
            evaluations.append({"label": "paired_no_l2", **paired["no_l2"]})
        row = {"case": case, "cores": cores, "scene": scene, "status": "ok",
               "selected_source": best[1], "selected": best[3],
               "selected_plan_hash": canonical_hash(best[2]),
               "paired": paired, "b_plan_hash": b_hash,
               "total_seconds": round(time.perf_counter() - started, 3),
               "official_evaluations": calls + (1 if scene == "L2" else 0),
               "budget": budget, "evaluations": evaluations, "errors": errors}
        job_path, plan_path = job_paths(run, case, cores, scene)
        atomic_json(plan_path, best[2])
        atomic_json(job_path, row)
        winners[scene], leaders_by_scene[scene] = best, leaders
    return "computed"


def singlecore(run, case):
    run = Path(run)
    path = run / "baseline" / "jobs" / f"{case}.json"
    if path.is_file():
        try:
            if read_json(path)["status"] == "ok":
                return "cached"
        except (ValueError, KeyError):
            pass
    graph, config = read_json(DATA / f"{case}.json"), settings()
    started = time.perf_counter()
    metric = baseline(graph, config)
    # The official one-core plan permits pure B/L2 comparison at core count 1.
    from singlecore_evaluate import build_singlecore_plan
    plan = build_singlecore_plan(graph)
    no_l2 = evaluate(graph, plan, "B", config)
    with_l2 = evaluate(graph, plan, "L2", config)
    row = {"case": case, "status": "ok", **metric,
           "paired_no_l2_makespan": no_l2["makespan"],
           "paired_with_l2_makespan": with_l2["makespan"],
           "paired_no_l2_added_copy_bytes": no_l2["added_copy_bytes"],
           "paired_with_l2_added_copy_bytes": with_l2["added_copy_bytes"],
           "paired_l2_speedup": no_l2["makespan"] / with_l2["makespan"],
           "paired_cache_hit_rate": with_l2["cache_hit_rate"],
           "evaluation_seconds": round(time.perf_counter() - started, 3)}
    atomic_json(path, row)
    return "computed"


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def summarize(run, cases, cores):
    run = Path(run)
    failures, baselines, summaries = [], [], {}
    for case in cases:
        try:
            row = read_json(run / "baseline" / "jobs" / f"{case}.json")
            if row["status"] != "ok":
                raise ValueError("failed")
            baselines.append(row)
        except (OSError, ValueError, KeyError) as exc:
            failures.append({"scene": "baseline", "case": case, "cores": 1, "error": str(exc)})
    write_csv(run / "baseline" / "singlecore.csv", baselines,
              ["case", "status", "makespan", "added_copy_bytes", "paired_no_l2_makespan",
               "paired_with_l2_makespan", "paired_no_l2_added_copy_bytes",
               "paired_with_l2_added_copy_bytes", "paired_l2_speedup",
               "paired_cache_hit_rate", "evaluation_seconds"])
    one = {row["case"]: row["makespan"] for row in baselines}
    for scene, folder in FOLDERS.items():
        rows = []
        for case in cases:
            for count in cores:
                try:
                    job = read_json(job_paths(run, case, count, scene)[0])
                    if job["status"] != "ok" or not complete_job(run, case, count, scene):
                        raise ValueError("failed or stale result")
                    selected = job["selected"]
                    pair = job.get("paired") or {}
                    no, yes = pair.get("no_l2") or {}, pair.get("with_l2") or {}
                    rows.append({"case": case, "cores": count, "singlecore_makespan": one.get(case, ""),
                                 "makespan": selected["makespan"],
                                 "speedup": one[case] / selected["makespan"] if case in one else "",
                                 "added_copy_bytes": selected["added_copy_bytes"],
                                 "selected_source": job["selected_source"],
                                 "total_seconds": job["total_seconds"],
                                 "paired_no_l2_makespan": no.get("makespan", ""),
                                 "paired_with_l2_makespan": yes.get("makespan", ""),
                                 "paired_no_l2_added_copy_bytes": no.get("added_copy_bytes", ""),
                                 "paired_with_l2_added_copy_bytes": yes.get("added_copy_bytes", ""),
                                 "paired_l2_speedup": pair.get("cache_speedup", ""),
                                 "paired_cache_hit_rate": yes.get("cache_hit_rate", ""),
                                 "cache_hit_rate": selected.get("cache_hit_rate", "")})
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    failures.append({"scene": scene, "case": case, "cores": count, "error": str(exc)})
        fields = ["case", "cores", "singlecore_makespan", "makespan", "speedup",
                  "added_copy_bytes", "selected_source", "total_seconds",
                  "paired_no_l2_makespan", "paired_with_l2_makespan",
                  "paired_no_l2_added_copy_bytes", "paired_with_l2_added_copy_bytes",
                  "paired_l2_speedup", "paired_cache_hit_rate", "cache_hit_rate"]
        write_csv(run / folder / "summary.csv", rows, fields)
        summaries[scene] = rows
    aggregate = []
    for scene, rows in summaries.items():
        for count in [1] + list(cores):
            current = [r for r in rows if r["cores"] == count]
            if count == 1:
                ratio = [r["paired_l2_speedup"] for r in baselines] if scene == "L2" else []
                aggregate.append({"scene": scene, "cores": 1, "count": len(baselines),
                                  "mean_speedup": 1 if len(baselines) == len(cases) else "",
                                  "mean_paired_l2_speedup": sum(ratio) / len(ratio) if ratio else ""})
            else:
                speeds = [r["speedup"] for r in current if r["speedup"] != ""]
                ratio = [r["paired_l2_speedup"] for r in current if r["paired_l2_speedup"] != ""]
                aggregate.append({"scene": scene, "cores": count, "count": len(current),
                                  "mean_speedup": sum(speeds) / len(speeds) if speeds else "",
                                  "mean_paired_l2_speedup": sum(ratio) / len(ratio) if ratio else ""})
    write_csv(run / "reports" / "aggregate.csv", aggregate,
              ["scene", "cores", "count", "mean_speedup", "mean_paired_l2_speedup"])
    write_csv(run / "reports" / "failures.csv", failures, ["scene", "case", "cores", "error"])
    return failures, summaries


def validate(run, replay=False):
    run = Path(run)
    manifest = read_json(run / "manifest.json")
    issues, checked = [], 0
    config = settings() if replay else None
    for case in manifest["cases"]:
        try:
            baseline_row = read_json(run / "baseline" / "jobs" / f"{case}.json")
            if baseline_row["status"] != "ok":
                raise ValueError("baseline failed")
        except (OSError, KeyError, ValueError) as exc:
            issues.append(f"baseline {case}: {exc}")
        graph = read_json(DATA / f"{case}.json")
        for cores in manifest["cores"]:
            found = {}
            for scene in FOLDERS:
                try:
                    job_path, plan_path = job_paths(run, case, cores, scene)
                    job, plan = read_json(job_path), read_json(plan_path)
                    if job["status"] != "ok" or len(plan["core_schedules"]) != cores:
                        raise ValueError("bad status or core count")
                    if canonical_hash(plan) != job["selected_plan_hash"]:
                        raise ValueError("plan hash mismatch")
                    derive_multicore_plan(graph, plan)
                    if not any(item.get("label") == job["selected_source"] and
                               score(item) == score(job["selected"])
                               for item in job["evaluations"]):
                        raise ValueError("selected score not in official evaluations")
                    if replay and score(evaluate(graph, plan, scene, config)) != score(job["selected"]):
                        raise ValueError("official replay mismatch")
                    found[scene] = (job, plan)
                    checked += 1
                except Exception as exc:
                    issues.append(f"{scene} {case} {cores}: {exc}")
            if "B" in found and "L2" in found:
                try:
                    b, b_plan = found["B"]
                    l2, _ = found["L2"]
                    pair = l2["paired"]
                    if l2["b_plan_hash"] != canonical_hash(b_plan) or score(pair["no_l2"]) != score(b["selected"]):
                        issues.append(f"stale B/L2 pair {case} {cores}")
                    if abs(pair["cache_speedup"] - pair["no_l2"]["makespan"] / pair["with_l2"]["makespan"]) > 1e-12:
                        issues.append(f"bad Cache speedup {case} {cores}")
                    if score(l2["selected"]) > score(pair["with_l2"]):
                        issues.append(f"L2 selected worse than paired B {case} {cores}")
                    if replay and score(evaluate(graph, b_plan, "L2", config)) != score(pair["with_l2"]):
                        issues.append(f"paired Cache replay mismatch {case} {cores}")
                except Exception as exc:
                    issues.append(f"invalid B/L2 pair {case} {cores}: {exc}")
    return {"status": "ok" if not issues else "error", "issues": issues,
            "checked_plans": checked, "replayed": checked if replay else 0}


def run_tasks(run, tasks, workers, timeout_minutes, budgets, baseline_phase=False):
    # Process isolation bounds memory leaks and permits hard wall-time limits.
    pending, active, failures = list(tasks), {}, []
    total, done = len(pending), 0
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    try:
        while pending or active:
            while pending and len(active) < workers:
                case, cores = pending.pop(0)
                cmd = [sys.executable, str(ROOT / "run.py"), "--_worker-run", str(run),
                       "--_worker-case", case, "--small-budget", str(budgets[0]),
                       "--large-budget", str(budgets[1])]
                cmd += ["--_worker-singlecore"] if baseline_phase else ["--_worker-core", str(cores)]
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, encoding="utf-8", errors="replace", env=env)
                active[process] = (case, cores, time.monotonic())
            for process, (case, cores, started) in list(active.items()):
                timeout = time.monotonic() - started >= timeout_minutes * 60
                if process.poll() is None and not timeout:
                    continue
                if timeout and process.poll() is None:
                    process.kill()
                output, _ = process.communicate()
                del active[process]
                done += 1
                if timeout or process.returncode:
                    error = f"timeout after {timeout_minutes} minutes" if timeout else f"exit {process.returncode}: {output[-2000:]}"
                    failures.append({"case": case, "cores": cores, "error": error})
                    print(f"ERROR {case} {cores}: {error}", flush=True)
                elif done <= 3 or done % 10 == 0 or done == total:
                    print(f"{'baseline' if baseline_phase else 'job'} {done}/{total} {case} {cores}", flush=True)
            if active:
                time.sleep(0.2)
    except KeyboardInterrupt:
        for process in active:
            process.kill()
            process.communicate()
        raise
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--cores", nargs="+", type=int)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--small-budget", type=int)
    parser.add_argument("--large-budget", type=int)
    parser.add_argument("--run", type=Path, help="resume existing run")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--timeout-minutes", type=int, default=120)
    parser.add_argument("--no-plots", action="store_true")
    parser.add_argument("--_worker-run", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_worker-case", help=argparse.SUPPRESS)
    parser.add_argument("--_worker-core", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--_worker-singlecore", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker_case:
        if args._worker_singlecore:
            print(singlecore(args._worker_run, args._worker_case))
        else:
            print(optimize(args._worker_run, args._worker_case, args._worker_core,
                           args.small_budget, args.large_budget))
        return 0
    prior = read_json(args.run / "manifest.json") if args.run else None
    cores = args.cores if args.cores is not None else (prior["cores"] if prior else [2, 3, 4, 5])
    small = args.small_budget if args.small_budget is not None else (prior["small_budget"] if prior else 72)
    large = args.large_budget if args.large_budget is not None else (prior["large_budget"] if prior else 44)
    available = sorted(path.stem for path in DATA.glob("case_*.json"))
    cases = sorted(set(args.cases if args.cases is not None else (prior["cases"] if prior else available)))
    if len(available) != 100 or not cases or not set(cases) <= set(available):
        parser.error("expected 100 official input graphs and valid selected cases")
    if (args.workers < 1 or small < 1 or large < 1 or args.timeout_minutes < 1 or
            cores != sorted(set(cores)) or any(core not in (2, 3, 4, 5) for core in cores)):
        parser.error("invalid workers, budget, timeout or cores")
    identity = {"cases": cases, "cores": cores, "small_budget": small, "large_budget": large,
                "config_sha256": file_hash(DATA / "config.txt"),
                "input_hashes": input_hashes(cases), "source_hashes": source_hashes()}
    if prior:
        run = args.run.resolve()
        for key, value in identity.items():
            if prior.get(key) != value:
                parser.error(f"resume mismatch: {key}")
        manifest = prior
    else:
        run = (args.output_root or ROOT / "runs") / (datetime.now().strftime("%Y%m%d_%H%M%S_") + secrets.token_hex(3))
        run.mkdir(parents=True, exist_ok=False)
        manifest = {"algorithm": "npu_5_independent", "created_at": datetime.now().astimezone().isoformat(), **identity}
    manifest["status"] = "running"
    manifest["workers"] = args.workers
    manifest["timeout_minutes"] = args.timeout_minutes
    atomic_json(run / "manifest.json", manifest)
    print(f"RUN={run}", flush=True)
    process_failures = []
    try:
        baseline_cases = []
        for case in cases:
            path = run / "baseline" / "jobs" / f"{case}.json"
            try:
                if read_json(path)["status"] == "ok":
                    continue
            except (OSError, ValueError, KeyError):
                pass
            baseline_cases.append(case)
        process_failures.extend(run_tasks(run, [(case, 1) for case in baseline_cases], args.workers,
                                          args.timeout_minutes, (small, large), True))
        pending = [(case, core) for case in cases for core in cores
                   if not all(complete_job(run, case, core, scene) for scene in FOLDERS)]
        process_failures.extend(run_tasks(run, pending, args.workers, args.timeout_minutes, (small, large)))
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        atomic_json(run / "manifest.json", manifest)
        print(f"Interrupted; resume with --run {run}", flush=True)
        return 130
    failures, _ = summarize(run, cases, cores)
    audit = validate(run) if not failures else {"status": "error", "issues": failures, "checked_plans": 0}
    if audit["status"] == "ok" and not args.no_plots:
        try:
            from reports import build_plots
            build_plots(run)
        except Exception as exc:
            audit["status"] = "error"
            audit["issues"].append(f"plot generation: {exc}")
    atomic_json(run / "reports" / "validation.json", audit)
    manifest["status"] = "complete" if not process_failures and audit["status"] == "ok" else "incomplete"
    required_plots = [run / "reports" / f"problem_{number}_speedup.png" for number in (1, 2, 3)]
    required_plots.append(run / "reports" / "problem_3_cache_gain.png")
    manifest["full_experiment_complete"] = bool(
        manifest["status"] == "complete" and len(cases) == 100 and cores == [2, 3, 4, 5]
        and not args.no_plots and all(path.is_file() for path in required_plots))
    manifest["finished_at"] = datetime.now().astimezone().isoformat()
    manifest["process_failures"] = process_failures
    atomic_json(run / "manifest.json", manifest)
    print(f"STATUS={manifest['status']}", flush=True)
    return 0 if manifest["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
