"""Eighth-version official search, machine sharding and resumable runner."""
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
from astra import search as search

FOLDERS = {"A": "problem_1", "B": "problem_2", "L2": "problem_3"}


def source_hashes():
    return {str(path.relative_to(ROOT.parent)): file_hash(path)
            for directory in (ROOT, CODE) for path in sorted(directory.glob("*.py"))}


def input_hashes(cases):
    return {case: file_hash(DATA / f"{case}.json") for case in cases}


def seed_hashes(seed_runs, cases, cores):
    hashes = {}
    for name, seed_run in seed_runs.items():
        if not seed_run:
            continue
        seed_run = Path(seed_run)
        for name_part in ('manifest.json', 'source_manifest_B_L2.json'):
            path = seed_run / name_part
            if path.is_file():
                hashes[f'{name}/{name_part}'] = file_hash(path)
        for case in cases:
            for count in cores:
                for scene in FOLDERS:
                    job_path, plan_path = job_paths(seed_run, case, count, scene)
                    for path in (job_path, plan_path):
                        if path.is_file():
                            hashes[f"{name}/{path.relative_to(seed_run).as_posix()}"] = file_hash(path)
    return hashes


def load_seed_candidates(seed_runs, case, cores, scene):
    seeds = []
    for name, seed_run in seed_runs.items():
        if not seed_run:
            continue
        job_path, plan_path = job_paths(Path(seed_run), case, cores, scene)
        try:
            job, plan = read_json(job_path), read_json(plan_path)
            if job.get("status") != "ok" or not isinstance(job.get("selected"), dict):
                continue
            expected = job.get("selected_plan_hash")
            if expected and expected != canonical_hash(plan):
                continue
            label = f"seed_{name}_{job.get('selected_source', 'selected')}"
            seeds.append({"label": label, "source": name, "plan": plan,
                          "reference_metrics": job['selected'],
                          "plan_hash": canonical_hash(plan),
                          "source_plan_sha256": file_hash(plan_path),
                          "source_job_sha256": file_hash(job_path)})
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return seeds


def restore_leaders(job, plan):
    pool = job.get("leader_pool") or [{"label": job["selected_source"],
                                       "plan": plan, "metrics": job["selected"]}]
    leaders = []
    for item in pool:
        candidate_plan, metrics = item["plan"], item["metrics"]
        leaders.append((score(metrics), item["label"], candidate_plan, metrics))
    return sorted(leaders, key=lambda item: (item[0], item[1]))[:4]


def serializable_leaders(leaders):
    return [{"label": label, "score": list(rank), "plan": plan,
             "plan_hash": canonical_hash(plan), "metrics": metrics}
            for rank, label, plan, metrics in leaders[:4]]


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
        if scene == "B" and not job.get("leader_pool"):
            return False
        if scene == "L2":
            b_job, b_plan = job_paths(run, case, cores, "B")
            return (job["b_plan_hash"] == canonical_hash(read_json(b_plan))
                    and job["paired"]["no_l2"]["makespan"] == read_json(b_job)["selected"]["makespan"])
        return True
    except (FileNotFoundError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return False


def optimize(run, case, cores, small_budget, large_budget, seed_runs=None, scenes=None):
    run = Path(run)
    scenes = scenes or tuple(FOLDERS)
    if all(complete_job(run, case, cores, scene) for scene in scenes):
        return "cached"
    graph = read_json(DATA / f"{case}.json")
    config = settings()
    count = sum(op["op"] not in {"COPY_IN", "COPY_OUT"} for op in graph["ops"])
    budget = small_budget if count <= 5000 else large_budget
    seed_runs = seed_runs or {}
    winners, leaders_by_scene = {}, {}
    for scene in scenes:
        if complete_job(run, case, cores, scene):
            job_path, plan_path = job_paths(run, case, cores, scene)
            job, plan = read_json(job_path), read_json(plan_path)
            winners[scene] = (score(job["selected"]), job["selected_source"], plan, job["selected"])
            leaders_by_scene[scene] = restore_leaders(job, plan)
            continue
        started = time.perf_counter()
        seeds = load_seed_candidates(seed_runs, case, cores, scene)
        a_seeds = load_seed_candidates(seed_runs, case, cores, "A") if scene != "A" else []
        extras = [(item["label"], item["plan"]) for item in seeds + a_seeds]
        # Use the same A reference on both machines, independent of whether
        # this worker also ran A. Current-scene rescoring is always required.
        paired, b_hash = None, None
        if scene == "L2":
            b_plan, b_metrics = winners["B"][2], winners["B"][3]
            b_hash = canonical_hash(b_plan)
            extras.insert(0, ("paired_b_plan", b_plan))
            extras.extend((f"b_candidate_{label}", candidate_plan)
                          for _, label, candidate_plan, _ in leaders_by_scene["B"][:4])
        best, leaders, evaluations, errors, counts = search(
            graph, cores, scene, config, budget, extras)
        if seeds:
            for seed in seeds:
                rescored = next((e for e in evaluations if e.get('plan_hash') == seed['plan_hash']), None)
                if rescored is None:
                    raise RuntimeError(f"Reference {scene} plan failed official reevaluation")
                if score(rescored) != score(seed['reference_metrics']):
                    raise RuntimeError(f'Reference {scene} score changed; check official evaluator and environment')
                if best[0] > score(rescored):
                    raise RuntimeError(f'{scene} incumbent regression')
        if scene == "L2":
            paired_eval = next((item for item in evaluations
                                if item.get("plan_hash") == b_hash), None)
            if paired_eval is None:
                raise RuntimeError(f"paired B plan was not officially evaluated: {case} {cores}")
            paired_cache = {key: value for key, value in paired_eval.items()
                            if key not in {"label", "plan_hash"}}
            paired = {"no_l2": b_metrics, "with_l2": paired_cache,
                      "cache_speedup": b_metrics["makespan"] / paired_cache["makespan"],
                      "evaluation_label": paired_eval["label"]}
            evaluations.append({"label": "paired_no_l2", **b_metrics})
        leader_pool = serializable_leaders(leaders)
        row = {"case": case, "cores": cores, "scene": scene, "status": "ok",
               "selected_source": best[1], "selected": best[3],
               "selected_plan_hash": canonical_hash(best[2]),
               "paired": paired, "b_plan_hash": b_hash,
               "leader_pool": leader_pool,
               "seed_sources": [{key: value for key, value in seed.items() if key != "plan"}
                                for seed in seeds + a_seeds],
               "evaluation_counts": counts,
               "official_evaluations": counts["official_evaluations"],
               "budget": budget, "total_seconds": round(time.perf_counter() - started, 3),
               "evaluations": evaluations, "errors": errors}
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
    manifest = read_json(run / 'manifest.json')
    for scene, folder in FOLDERS.items():
        scene_cases = manifest.get('scene_cases', {}).get(scene, cases)
        if not scene_cases:
            continue
        rows = []
        for case in scene_cases:
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
                                 "partition_added_copy_bytes": selected.get('partition_added_copy_bytes', ''),
                                 "spill_added_copy_bytes": selected.get('spill_added_copy_bytes', ''),
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
                  "added_copy_bytes", "partition_added_copy_bytes", "spill_added_copy_bytes",
                  "selected_source", "total_seconds",
                  "paired_no_l2_makespan", "paired_with_l2_makespan",
                  "paired_no_l2_added_copy_bytes", "paired_with_l2_added_copy_bytes",
                  "paired_l2_speedup", "paired_cache_hit_rate", "cache_hit_rate"]
        write_csv(run / folder / "summary.csv", rows, fields)
        summaries[scene] = rows
    aggregate = []
    for scene, rows in summaries.items():
        scene_baselines = [r for r in baselines if r['case'] in manifest.get('scene_cases', {}).get(scene, cases)]
        for count in [1] + list(cores):
            current = [r for r in rows if r["cores"] == count]
            if count == 1:
                ratio = [r["paired_l2_speedup"] for r in scene_baselines] if scene == "L2" else []
                aggregate.append({"scene": scene, "cores": 1, "count": len(scene_baselines),
                                  "mean_speedup": 1 if len(scene_baselines) == len(manifest.get("scene_cases", {}).get(scene, cases)) else "",
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
                if case not in manifest.get('scene_cases', {}).get(scene, manifest['cases']):
                    continue
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
                    counts = job.get("evaluation_counts", {})
                    if counts.get("official_evaluations") != job.get("official_evaluations"):
                        raise ValueError("official evaluation count mismatch")
                    if job["official_evaluations"] > job["budget"]:
                        raise ValueError("official evaluation budget exceeded")
                    if counts.get("official_valid", 0) + counts.get("official_invalid", 0) != job["official_evaluations"]:
                        raise ValueError("official valid/invalid count mismatch")
                    if scene == "B":
                        pool = job.get("leader_pool", [])
                        if not pool or len(pool) > 4:
                            raise ValueError("missing or oversized persistent B leader pool")
                        ranks = []
                        for item in pool:
                            derive_multicore_plan(graph, item["plan"])
                            if canonical_hash(item["plan"]) != item["plan_hash"]:
                                raise ValueError("B leader plan hash mismatch")
                            if not any(e.get("label") == item["label"] and
                                       score(e) == score(item["metrics"])
                                       for e in job["evaluations"]):
                                raise ValueError("B leader score missing from official evaluations")
                            ranks.append(tuple(item["score"]))
                        if ranks != sorted(ranks):
                            raise ValueError("B leader pool is not score ordered")
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


def run_tasks(run, tasks, workers, timeout_minutes, budgets, source_args=(), baseline_phase=False):
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
                cmd += list(source_args)
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


def scene_selection(cases, machine=None, scenes=None):
    if machine == '1':
        return {'A': list(cases), 'B': [c for c in cases if int(c[-3:]) > 50],
                'L2': [c for c in cases if int(c[-3:]) > 50]}
    if machine == '2':
        return {'A': [], 'B': list(cases),
                'L2': [c for c in cases if int(c[-3:]) <= 50]}
    chosen = set(scenes or FOLDERS)
    if 'L2' in chosen:
        chosen.add('B')
    return {s: list(cases) if s in chosen else [] for s in FOLDERS}


def finish_run(run, manifest, process_failures=(), no_plots=False):
    failures, _ = summarize(run, manifest['cases'], manifest['cores'])
    audit = validate(run) if not failures else {'status': 'error', 'issues': failures, 'checked_plans': 0}
    if audit['status'] == 'ok' and not no_plots:
        try:
            from reports import build_plots
            build_plots(run)
        except Exception as exc:
            audit['status'] = 'error'
            audit['issues'].append(f'plot generation: {exc}')
    atomic_json(run / 'reports/validation.json', audit)
    manifest['status'] = 'complete' if not process_failures and audit['status'] == 'ok' else 'incomplete'
    for scene, folder in FOLDERS.items():
        manifest[folder + '_complete'] = bool(manifest['status'] == 'complete'
            and len(manifest['scene_cases'][scene]) == 100 and manifest['cores'] == [2, 3, 4, 5])
    required = [run / 'reports' / f'problem_{n}_speedup.png' for n in (1, 2, 3)]
    required += [run / 'reports/problem_3_cache_gain.png', run / 'reports/problem_3_comparison.png']
    manifest['full_experiment_complete'] = bool(
        all(manifest[f'problem_{n}_complete'] for n in (1, 2, 3))
        and not no_plots and all(p.is_file() for p in required))
    manifest['finished_at'] = datetime.now().astimezone().isoformat()
    manifest['process_failures'] = list(process_failures)
    atomic_json(run / 'manifest.json', manifest)
    return manifest['status'] == 'complete'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--cores', nargs='+', type=int)
    parser.add_argument('--machine', choices=('1', '2'))
    parser.add_argument('--scenes', nargs='+', choices=tuple(FOLDERS))
    parser.add_argument('--workers', type=int, default=12)
    parser.add_argument('--small-budget', type=int)
    parser.add_argument('--large-budget', type=int)
    parser.add_argument('--run', type=Path, help='resume a printed RUN directory')
    parser.add_argument('--output-root', type=Path)
    parser.add_argument('--seed-run', type=Path)
    parser.add_argument('--no-reference', action='store_true')
    parser.add_argument('--timeout-minutes', type=int, default=180)
    parser.add_argument('--no-plots', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='print job counts without creating or running experiments')
    parser.add_argument('--_worker-run', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--_worker-case', help=argparse.SUPPRESS)
    parser.add_argument('--_worker-core', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--_worker-singlecore', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker_case:
        manifest = read_json(args._worker_run / 'manifest.json')
        if args._worker_singlecore:
            print(singlecore(args._worker_run, args._worker_case))
        else:
            scenes = [s for s in FOLDERS if args._worker_case in manifest['scene_cases'][s]]
            print(optimize(args._worker_run, args._worker_case, args._worker_core,
                           manifest['small_budget'], manifest['large_budget'],
                           manifest['seed_runs'], scenes))
        return 0
    prior = read_json(args.run / 'manifest.json') if args.run else None
    if args.machine and args.scenes:
        parser.error('--machine and --scenes are mutually exclusive')
    available = sorted(p.stem for p in DATA.glob('case_*.json'))
    cases = sorted(set(args.cases if args.cases is not None else prior['cases'] if prior else available))
    cores = args.cores if args.cores is not None else prior['cores'] if prior else [2, 3, 4, 5]
    small = args.small_budget if args.small_budget is not None else prior['small_budget'] if prior else 120
    large = args.large_budget if args.large_budget is not None else prior['large_budget'] if prior else 80
    if len(available) != 100 or not cases or not set(cases) <= set(available):
        parser.error('expected 100 official graphs and valid selected cases')
    if (args.workers < 1 or small < 1 or large < 1 or args.timeout_minutes < 1
            or not cores or cores != sorted(set(cores)) or any(c not in (2, 3, 4, 5) for c in cores)):
        parser.error('invalid workers, budgets, timeout or cores')
    selection = (prior['scene_cases'] if prior and not args.machine and not args.scenes else
                 scene_selection(cases, args.machine, args.scenes))
    if args.dry_run:
        print(json.dumps({'machine': args.machine, 'cases': len(cases), 'cores': cores,
                          'baseline_jobs': len(cases),
                          'scene_jobs': {s: len(cs)*len(cores) for s, cs in selection.items()},
                          'case_ranges': {s: [cs[0], cs[-1]] if cs else [] for s, cs in selection.items()},
                          'small_budget': small, 'large_budget': large}, indent=2))
        return 0
    if args.no_reference and args.seed_run:
        parser.error('--no-reference and --seed-run are mutually exclusive')
    seed_path = (args.seed_run or (Path(prior['seed_runs']['reference'])
                 if prior and prior.get('seed_runs') else ROOT / 'reference'))
    seed_runs = {} if args.no_reference or (prior and not prior.get('seed_runs') and not args.seed_run) else {'reference': str(seed_path.resolve())}
    if seed_runs:
        if selection['L2'] and min(small, large) < 2:
            parser.error('L2 with references requires budget >= 2 for historical winner and paired B')
        seed_manifest = read_json(seed_path / 'manifest.json')
        if seed_manifest['config_sha256'] != file_hash(DATA / 'config.txt'):
            parser.error('reference config mismatch')
        for case in cases:
            if seed_manifest['input_hashes'].get(case) != file_hash(DATA / f'{case}.json'):
                parser.error(f'reference graph mismatch: {case}')
            for core in cores:
                for scene in FOLDERS:
                    if scene != 'A' and case not in selection[scene]:
                        continue
                    if not load_seed_candidates(seed_runs, case, core, scene):
                        parser.error(f'missing or corrupt reference {scene} plan: {case} {core}')
    identity = {'cases': cases, 'cores': cores, 'scene_cases': selection,
                'small_budget': small, 'large_budget': large,
                'config_sha256': file_hash(DATA / 'config.txt'), 'input_hashes': input_hashes(cases),
                'source_hashes': source_hashes(), 'seed_runs': seed_runs,
                'seed_hashes': seed_hashes(seed_runs, cases, cores)}
    if prior:
        run = args.run.resolve()
        for key, value in identity.items():
            if prior.get(key) != value:
                parser.error(f'resume mismatch: {key}')
        manifest = prior
    else:
        run = (args.output_root or ROOT / 'runs') / (datetime.now().strftime('%Y%m%d_%H%M%S_') + secrets.token_hex(3))
        run = run.resolve()
        run.mkdir(parents=True, exist_ok=False)
        manifest = {'algorithm': 'NPU_8', 'machine': args.machine,
                    'created_at': datetime.now().astimezone().isoformat(), **identity}
    manifest.update(status='running', workers=args.workers, timeout_minutes=args.timeout_minutes)
    atomic_json(run / 'manifest.json', manifest)
    print(f'RUN={run}', flush=True)
    print('SCENES=' + json.dumps({s: len(cs)*len(cores) for s, cs in selection.items()}), flush=True)
    failures = []
    try:
        pending_baseline = []
        for case in cases:
            try:
                if read_json(run / 'baseline/jobs' / f'{case}.json')['status'] == 'ok':
                    continue
            except (OSError, ValueError, KeyError):
                pass
            pending_baseline.append((case, 1))
        failures.extend(run_tasks(run, pending_baseline, args.workers, args.timeout_minutes,
                                  (small, large), baseline_phase=True))
        pending = [(case, core) for case in cases for core in cores if not all(
            complete_job(run, case, core, s) for s in FOLDERS if case in selection[s])]
        failures.extend(run_tasks(run, pending, args.workers, args.timeout_minutes, (small, large)))
    except KeyboardInterrupt:
        manifest['status'] = 'interrupted'
        atomic_json(run / 'manifest.json', manifest)
        print(f'Interrupted; resume with --run "{run}"', flush=True)
        return 130
    ok = finish_run(run, manifest, failures, args.no_plots)
    print(f"STATUS={manifest['status']}", flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())


