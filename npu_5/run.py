"""Run all three NPU scheduling scenes with official evaluation and v4 fallback.

The default run is intended for the user's Windows CPU. Every selected plan is
an officially evaluated plan; estimates only order candidate exploration.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ATTACHMENT = ROOT.parent
V4 = ATTACHMENT / "多核调度_第四版"
V4_RUN = V4 / "runs" / "20260923_141723_v4"
if str(V4) not in sys.path:
    sys.path.insert(0, str(V4))

from worker import atomic_json, read_json, settings, attempt, score, run_singlecore  # noqa: E402
from run_all import summarize  # noqa: E402
from stub_multicore_cut_and_schedule import derive_multicore_plan  # noqa: E402
from solver import initial_candidates, neighbors  # noqa: E402

FOLDERS = {"A": "problem_1", "B": "problem_2", "L2": "problem_3"}
SOURCES = ("run.py", "solver.py")
BORROWED = ("worker.py", "solver_v2.py", "solver_v4.py",
            "solver_idblocks.py", "run_all.py", "reports.py")


def canonical_hash(value):
    data = json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def source_hash(path):
    # Windows checkouts may use CRLF; code identity should not depend on that.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def source_hashes():
    result = {f"npu_5/{name}": source_hash(ROOT / name) for name in SOURCES}
    result.update({f"v4/{name}": source_hash(V4 / name) for name in BORROWED})
    result.update({f"code/{path.name}": source_hash(path)
                   for path in sorted((ATTACHMENT / "code").glob("*.py"))})
    return result


def input_hashes(cases):
    return {case: canonical_hash(read_json(ATTACHMENT / "data" / f"{case}.json"))
            for case in cases}


def _case_core_complete(run, case, cores):
    stem = f"{case}_{cores}core.json"
    try:
        records = {}
        for scene, folder in FOLDERS.items():
            job = read_json(run / folder / "jobs" / stem)
            plan = read_json(run / folder / "plans" / stem)
            if (job.get("status") != "ok" or
                    job.get("selected_plan_hash") != canonical_hash(plan)):
                return False
            records[scene] = job
        b_plan = read_json(run / "problem_2" / "plans" / stem)
        l2 = records["L2"]
        return (l2.get("b_plan_hash") == canonical_hash(b_plan) and
                (l2.get("paired") or {}).get("no_l2", {}).get("makespan") ==
                records["B"]["selected"]["makespan"])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return False


def _evaluate_scene(graph, config, scene, seed_plan, seed_job, cores,
                    budget, extra=(), preevaluated=()):
    evaluations, errors = [], []
    seen = set()
    leaders = []
    official_calls = 0

    def add(label, plan, metrics=None):
        nonlocal official_calls
        digest = canonical_hash(plan)
        if digest in seen:
            return
        seen.add(digest)
        if metrics is None:
            if official_calls >= budget:
                return
            try:
                derive_multicore_plan(graph, plan)
            except Exception as exc:
                errors.append({"label": label,
                               "error": f"invalid candidate: {type(exc).__name__}: {exc}"})
                return
            official_calls += 1
            metrics = attempt(graph, plan, scene, config, label,
                              evaluations, errors)
        else:
            evaluations.append({"label": label, **metrics})
        if metrics is not None:
            leaders.append((score(metrics), label, plan, metrics))
            leaders.sort(key=lambda item: (item[0], item[1]))
            del leaders[4:]

    # The incumbent is re-evaluated with the current official code and fixed
    # config. Its saved v4 score is reference data, never blindly accepted.
    add("v4_seed", seed_plan)
    if not leaders:
        raise RuntimeError(f"v4 seed invalid under official {scene}: {errors}")
    for label, plan, metrics in preevaluated:
        add(label, plan, metrics)
    for label, plan in extra:
        add(label, plan)
    try:
        for label, plan in initial_candidates(graph, cores, scene, config):
            if official_calls >= budget:
                break
            add(label, plan)
    except Exception as exc:
        errors.append({"label": "candidate_generation",
                       "error": f"{type(exc).__name__}: {exc}"})

    # Multi-start, two rounds: unlike v4 this can move or merge individual
    # subgraphs. Rejected plans are logged but cannot displace the incumbent.
    starts = leaders[:2]
    for _, label, plan, _ in starts:
        current = plan
        for round_id in range(2):
            if official_calls >= budget:
                break
            old_best = leaders[0][0]
            try:
                for move_label, candidate in neighbors(graph, current, scene):
                    if official_calls >= budget:
                        break
                    add(f"local{round_id+1}_{label}_{move_label}", candidate)
            except Exception as exc:
                errors.append({"label": f"local_generation_{label}",
                               "error": f"{type(exc).__name__}: {exc}"})
            if leaders[0][0] >= old_best:
                break
            current = leaders[0][2]
    best = leaders[0]
    if score(best[3]) > score(seed_job["selected"]):
        # Saved v4 metrics are used only as an audit signal. Differences can
        # arise if source/data changed, and must not be hidden by old scores.
        errors.append({"label": "v4_comparison",
                       "error": "current official seed score is worse than saved v4"})
    return best, leaders, evaluations, errors, official_calls


def optimize(run_text, case, cores, small_budget, large_budget):
    run = Path(run_text)
    if _case_core_complete(run, case, cores):
        return case, cores, "cached"
    graph = read_json(ATTACHMENT / "data" / f"{case}.json")
    config = settings()
    count = sum(op["op"] not in {"COPY_IN", "COPY_OUT"} for op in graph["ops"])
    budget = small_budget if count <= 5000 else large_budget
    stem = f"{case}_{cores}core.json"
    winners = {}
    leaders_by_scene = {}
    changes = {}
    for scene, folder in FOLDERS.items():
        started = time.perf_counter()
        old = read_json(V4_RUN / folder / "jobs" / stem)
        seed = read_json(V4_RUN / folder / "plans" / stem)
        extra, preevaluated = [], []
        paired = None
        b_hash = None
        if scene == "B":
            extra.append(("a_selected", winners["A"][2]))
        elif scene == "L2":
            b_plan = winners["B"][2]
            b_metric = winners["B"][3]
            b_hash = canonical_hash(b_plan)
            # This official pair isolates Cache benefit from changing plans.
            from worker import official_evaluate  # noqa: E402
            pair_started = time.perf_counter()
            cache = official_evaluate(graph, b_plan, "L2", config)
            cache["evaluation_seconds"] = round(time.perf_counter() - pair_started, 3)
            paired = {"no_l2": b_metric, "with_l2": cache,
                      "cache_speedup": b_metric["makespan"] / cache["makespan"]}
            preevaluated.append(("paired_b_plan", b_plan, cache))
            extra.append(("a_selected", winners["A"][2]))
            for _, label, plan, _ in leaders_by_scene["B"][:4]:
                extra.append((f"b_candidate_{label}", plan))
        best, leaders, evaluations, errors, calls = _evaluate_scene(
            graph, config, scene, seed, old, cores, budget,
            extra=extra, preevaluated=preevaluated)
        if scene == "L2":
            evaluations.append({"label": "paired_no_l2", **paired["no_l2"]})
        row = {
            "case": case, "cores": cores, "scene": scene, "status": "ok",
            "selected_source": best[1], "selected": best[3],
            "selected_plan_hash": canonical_hash(best[2]),
            "v1": old.get("v1"), "paired": paired, "b_plan_hash": b_hash,
            "generation_seconds": None,
            "total_seconds": round(time.perf_counter() - started, 3),
            "official_evaluations": calls + (1 if scene == "L2" else 0),
            "budget": budget, "evaluations": evaluations, "errors": errors,
            "v4_selected": old["selected"],
            "v4_plan_hash": canonical_hash(seed),
        }
        atomic_json(run / folder / "plans" / stem, best[2])
        atomic_json(run / folder / "jobs" / stem, row)
        winners[scene] = best
        leaders_by_scene[scene] = leaders
        changes[scene] = round(old["selected"]["makespan"] / best[3]["makespan"], 4)
    return case, cores, changes


def validate(run, replay=False):
    manifest = read_json(run / "manifest.json")
    config = settings() if replay else None
    issues = []
    checked = 0
    for case in manifest["cases"]:
        graph = read_json(ATTACHMENT / "data" / f"{case}.json")
        try:
            baseline = read_json(run / "baseline" / "jobs" / f"{case}.json")
            old_baseline = read_json(V4_RUN / "baseline" / "jobs" / f"{case}.json")
            if baseline.get("status") != "ok" or baseline["makespan"] != old_baseline["makespan"]:
                issues.append(f"single-core baseline mismatch {case}")
        except Exception as exc:
            issues.append(f"missing single-core baseline {case}: {exc}")
        for cores in manifest["cores"]:
            stem = f"{case}_{cores}core.json"
            records = {}
            for scene, folder in FOLDERS.items():
                try:
                    job = read_json(run / folder / "jobs" / stem)
                    plan = read_json(run / folder / "plans" / stem)
                    old = read_json(V4_RUN / folder / "jobs" / stem)
                    derive_multicore_plan(graph, plan)
                    if len(plan["core_schedules"]) != cores:
                        raise ValueError("wrong core count")
                    if job["selected_plan_hash"] != canonical_hash(plan):
                        raise ValueError("selected plan hash mismatch")
                    if not any(item.get("label") == job["selected_source"] and
                               score(item) == score(job["selected"])
                               for item in job.get("evaluations", [])):
                        raise ValueError("selected metrics absent from official evaluations")
                    if score(job["selected"]) > score(old["selected"]):
                        issues.append(f"regression vs v4 {scene} {case} {cores}")
                    if replay:
                        from worker import official_evaluate  # noqa: E402
                        actual = official_evaluate(graph, plan, scene, config)
                        if score(actual) != score(job["selected"]):
                            issues.append(f"replay mismatch {scene} {case} {cores}")
                    records[scene] = (job, plan)
                    checked += 1
                except Exception as exc:
                    issues.append(f"invalid {scene} {case} {cores}: {exc}")
            if "B" in records and "L2" in records:
                b, b_plan = records["B"]
                l2, _ = records["L2"]
                if l2["b_plan_hash"] != canonical_hash(b_plan):
                    issues.append(f"stale B/L2 pair {case} {cores}")
                if l2["paired"]["no_l2"]["makespan"] != b["selected"]["makespan"]:
                    issues.append(f"B/L2 metric mismatch {case} {cores}")
                if l2["paired"]["no_l2"]["added_copy_bytes"] != b["selected"]["added_copy_bytes"]:
                    issues.append(f"B/L2 movement mismatch {case} {cores}")
                paired_ratio = (l2["paired"]["no_l2"]["makespan"] /
                                l2["paired"]["with_l2"]["makespan"])
                if abs(l2["paired"]["cache_speedup"] - paired_ratio) > 1e-12:
                    issues.append(f"B/L2 speedup mismatch {case} {cores}")
                if score(l2["selected"]) > score(l2["paired"]["with_l2"]):
                    issues.append(f"L2 worse than paired {case} {cores}")
    return {"status": "ok" if not issues else "error",
            "issues": issues, "checked_plans": checked,
            "replayed": checked if replay else 0}


def _comparison(run, cases, cores):
    path = run / "reports" / "v4_v5_comparison.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=(
            "scene", "case", "cores", "v4_makespan", "v5_makespan",
            "v4_added_copy_bytes", "v5_added_copy_bytes", "time_reduction"))
        writer.writeheader()
        for scene, folder in FOLDERS.items():
            for case in cases:
                for core in cores:
                    stem = f"{case}_{core}core.json"
                    old = read_json(V4_RUN / folder / "jobs" / stem)["selected"]
                    new = read_json(run / folder / "jobs" / stem)["selected"]
                    writer.writerow({"scene": scene, "case": case, "cores": core,
                                     "v4_makespan": old["makespan"],
                                     "v5_makespan": new["makespan"],
                                     "v4_added_copy_bytes": old["added_copy_bytes"],
                                     "v5_added_copy_bytes": new["added_copy_bytes"],
                                     "time_reduction": 1 - new["makespan"] / old["makespan"]})


def _run_phase(run, tasks, workers, timeout_minutes, budgets, baseline=False):
    """Run bounded child processes so one expensive graph cannot stall all work."""
    pending = deque(tasks)
    active = {}
    failures = []
    completed = 0
    total = len(tasks)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    try:
        while pending or active:
            while pending and len(active) < workers:
                case, core = pending.popleft()
                cmd = [sys.executable, str(Path(__file__).resolve()),
                       "--_worker-run", str(run), "--_worker-case", case,
                       "--small-budget", str(budgets[0]),
                       "--large-budget", str(budgets[1])]
                if baseline:
                    cmd.append("--_worker-singlecore")
                else:
                    cmd += ["--_worker-core", str(core)]
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True,
                                           encoding="utf-8", errors="replace",
                                           env=env)
                active[process] = (case, core, time.monotonic())
            for process, (case, core, started) in list(active.items()):
                timed_out = time.monotonic() - started >= timeout_minutes * 60
                if process.poll() is None and not timed_out:
                    continue
                if timed_out and process.poll() is None:
                    process.kill()
                output, _ = process.communicate()
                del active[process]
                completed += 1
                if timed_out or process.returncode:
                    error = (f"timeout after {timeout_minutes} minutes" if timed_out
                             else f"exit {process.returncode}: {output[-1500:]}")
                    failures.append({"case": case, "cores": core, "error": error})
                    print(f"ERROR {case} {core}: {error}", flush=True)
                else:
                    print(f"{('singlecore' if baseline else 'job')} "
                          f"{completed}/{total} {case} {core}: {output.strip()}",
                          flush=True)
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
    parser.add_argument("--cores", nargs="+", type=int, default=[2, 3, 4, 5])
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--small-budget", type=int, default=72)
    parser.add_argument("--large-budget", type=int, default=44)
    parser.add_argument("--run", type=Path, help="resume an existing npu_5 run")
    parser.add_argument("--output-root", type=Path,
                        help="directory for new runs; defaults to npu_5/runs")
    parser.add_argument("--no-plots", action="store_true")
    parser.add_argument("--timeout-minutes", type=int, default=120,
                        help="maximum wall time for each case/core worker")
    parser.add_argument("--_worker-run", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_worker-case", help=argparse.SUPPRESS)
    parser.add_argument("--_worker-core", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--_worker-singlecore", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker_case:
        if not args._worker_run:
            parser.error("worker run directory missing")
        if args._worker_singlecore:
            result = run_singlecore(str(args._worker_run), args._worker_case)
            print(result["makespan"])
        else:
            if args._worker_core is None:
                parser.error("worker core count missing")
            print(optimize(str(args._worker_run), args._worker_case,
                           args._worker_core, args.small_budget,
                           args.large_budget))
        return 0
    if (args.workers < 1 or args.small_budget < 2 or args.large_budget < 2 or
            args.timeout_minutes < 1 or
            sorted(set(args.cores)) != sorted(args.cores) or
            any(core not in (2, 3, 4, 5) for core in args.cores)):
        parser.error("workers/budgets must be positive; cores must be distinct 2..5")
    available = sorted(path.stem for path in (ATTACHMENT / "data").glob("case_*.json"))
    if len(available) != 100:
        parser.error(f"expected 100 official cases, found {len(available)}")
    if not V4_RUN.is_dir() or read_json(V4_RUN / "manifest.json").get("status") != "complete":
        parser.error("complete v4 seed run is missing")
    cases = sorted(set(args.cases or available))
    if not cases or any(case not in available for case in cases):
        parser.error("unknown or empty case list")
    config_hash = source_hash(ATTACHMENT / "data" / "config.txt")
    if config_hash != read_json(V4_RUN / "manifest.json")["config_sha256"]:
        parser.error("fixed official config differs from v4 seed run")
    hashes = source_hashes()
    graphs = input_hashes(cases)
    if args.run:
        run = args.run.resolve()
        manifest = read_json(run / "manifest.json")
        expected = {"cases": cases, "cores": args.cores,
                    "config_sha256": config_hash, "input_hashes": graphs,
                    "source_hashes": hashes,
                    "small_budget": args.small_budget,
                    "large_budget": args.large_budget}
        for key, value in expected.items():
            if manifest.get(key) != value:
                parser.error(f"resume mismatch: {key}")
    else:
        output_root = args.output_root or ROOT / "runs"
        run = output_root / (datetime.now().strftime("%Y%m%d_%H%M%S_")
                             + secrets.token_hex(3))
        run.mkdir(parents=True)
        manifest = {"algorithm": "npu_5_official_multiscene_search",
                    "created_at": datetime.now().astimezone().isoformat(),
                    "cases": cases, "cores": args.cores,
                    "config_sha256": config_hash, "input_hashes": graphs,
                    "source_hashes": hashes, "seed_run": str(V4_RUN),
                    "small_budget": args.small_budget,
                    "large_budget": args.large_budget}
    manifest["status"] = "running"
    manifest["workers"] = args.workers
    atomic_json(run / "manifest.json", manifest)
    print(f"RUN={run}", flush=True)
    failures = []
    try:
        baseline_cases = [case for case in cases if not (
            (run / "baseline" / "jobs" / f"{case}.json").is_file() and
            read_json(run / "baseline" / "jobs" / f"{case}.json").get("status") == "ok")]
        if baseline_cases:
            failures += _run_phase(run, [(case, 1) for case in baseline_cases],
                                   args.workers, args.timeout_minutes,
                                   (args.small_budget, args.large_budget),
                                   baseline=True)
        failures += _run_phase(run, [(case, core) for case in cases
                                    for core in args.cores], args.workers,
                               args.timeout_minutes,
                               (args.small_budget, args.large_budget))
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        atomic_json(run / "manifest.json", manifest)
        print(f"Interrupted. Resume with --run {run}", flush=True)
        return 130
    errors, _ = summarize(run, cases, args.cores)
    if not errors:
        _comparison(run, cases, args.cores)
    if not errors and not args.no_plots:
        try:
            from reports import build_plots  # noqa: E402
            build_plots(run)
        except Exception as exc:
            errors.append({"phase": "plots", "error": str(exc)})
    result = validate(run) if not errors else {
        "status": "error", "issues": [str(item) for item in errors],
        "checked_plans": 0, "replayed": 0}
    atomic_json(run / "reports" / "validation.json", result)
    manifest["status"] = ("complete" if not failures and result["status"] == "ok"
                          else "incomplete")
    manifest["finished_at"] = datetime.now().astimezone().isoformat()
    manifest["failures"] = failures
    atomic_json(run / "manifest.json", manifest)
    print(f"STATUS={manifest['status']} validation={result['status']}", flush=True)
    return 0 if manifest["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
