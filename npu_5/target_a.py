"""Budgeted, seeded experiments for selected problem-one cases.

This runner never changes a historical run. It evaluates only new candidate
plans and keeps the historical official result as a verified incumbent.
"""
from __future__ import annotations

import argparse
import heapq
import multiprocessing
import secrets
import time
from datetime import datetime
from pathlib import Path

from official import (ROOT, CODE, DATA, atomic_json, canonical_hash,
                      derive_multicore_plan, file_hash, read_json, settings,
                      score, evaluate)
from graph import graph_view
from partition import _partition, _schedule_blocks, _topological_orders
from run import job_paths, source_hashes, input_hashes, summarize, validate
from solver import a_neighbors, component_candidates
from affinity_a import affinity_candidates, chain_candidates


def _evaluation_worker(connection, graph, plan, config):
    try:
        started = time.perf_counter()
        metrics = evaluate(graph, plan, "A", config)
        metrics["evaluation_seconds"] = round(time.perf_counter() - started, 3)
        connection.send(("ok", metrics))
    except Exception as exc:
        connection.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


def bounded_attempt(graph, plan, config, label, deadline, evaluations, errors):
    """Isolate each official simulation so the case deadline can interrupt it."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_evaluation_worker,
                              args=(child, graph, plan, config))
    started = time.perf_counter()
    try:
        process.start()
        child.close()
        if not parent.poll(max(0, deadline - time.perf_counter())):
            errors.append({"label": label, "error": "TimeoutError: case deadline reached",
                           "evaluation_seconds": round(time.perf_counter() - started, 3)})
            return None
        status, result = parent.recv()
        if status == "ok":
            evaluations.append({"label": label, **result})
            return result
        errors.append({"label": label, "error": result})
        return None
    except (EOFError, OSError) as exc:
        errors.append({"label": label, "error": f"{type(exc).__name__}: {exc}"})
        return None
    finally:
        if process.pid is not None:
            process.join(timeout=0.1)
            if process.is_alive():
                process.terminate()
                process.join()
        parent.close()
        child.close()


def seed_case(seed_run, case, cores):
    manifest = read_json(seed_run / "manifest.json")
    if manifest.get("status") != "complete" or "A" not in manifest.get("scenes", []):
        raise ValueError("seed run has no complete problem-one results")
    audit = read_json(seed_run / "reports" / "validation.json")
    if audit.get("status") != "ok":
        raise ValueError("seed run failed its saved validation")
    if case not in manifest["cases"] or cores not in manifest["cores"]:
        raise ValueError(f"seed run lacks {case} {cores}core")
    if manifest["config_sha256"] != file_hash(DATA / "config.txt"):
        raise ValueError("seed configuration differs from current configuration")
    if manifest["input_hashes"][case] != file_hash(DATA / f"{case}.json"):
        raise ValueError(f"seed graph differs for {case}")
    # Algorithm code may change; the contest evaluator must not.
    for path in CODE.glob("*.py"):
        name = str(path.relative_to(ROOT.parent))
        if manifest["source_hashes"].get(name) != file_hash(path):
            raise ValueError(f"official evaluator changed: {name}")
    old_job_path, old_plan_path = job_paths(seed_run, case, cores, "A")
    job, plan = read_json(old_job_path), read_json(old_plan_path)
    baseline = read_json(seed_run / "baseline" / "jobs" / f"{case}.json")
    if (job["status"] != "ok" or baseline["status"] != "ok"
            or job["selected_plan_hash"] != canonical_hash(plan)):
        raise ValueError(f"invalid historical result for {case}")
    if not any(entry.get("label") == job["selected_source"]
               and "makespan" in entry and score(entry) == score(job["selected"])
               for entry in job["evaluations"]):
        raise ValueError(f"seed score lacks an official candidate for {case}")
    derive_multicore_plan(read_json(DATA / f"{case}.json"), plan)
    return job, plan, baseline


def one_task_plan(graph, cores):
    nodes = [op["id"] for op in graph["ops"]
             if op["op"] not in {"COPY_IN", "COPY_OUT"}]
    return {"node_to_subgraph": {str(node): 0 for node in nodes},
            "core_schedules": [[0]] + [[] for _ in range(cores - 1)]}


def coarse_candidates(graph, cores, config):
    """Try a few large dependency-aware topological blocks, including branches."""
    ops, order, preds = graph_view(graph)
    orders, rank, output_size = _topological_orders(ops, order, preds)
    capacity = sum(config["capacity"].values())
    candidates = []
    for mode, arranged in orders.items():
        for blocks in (2, 3, 5, 8, 16):
            target = max(1, round(len(arranged) / blocks))
            pieces, block_of = _partition(arranged, ops, preds, output_size,
                                          target, capacity)
            plan, proxy = _schedule_blocks(pieces, block_of, ops, preds, rank,
                                           cores, "A", config, output_size)
            candidates.append((proxy, f"coarse_{mode}_{blocks}", plan))
    # Keep at least one candidate at each granularity before comparing proxies.
    preferred = []
    for blocks in (2, 3, 5, 8, 16):
        preferred.append(min((x for x in candidates if x[1].endswith(f"_{blocks}")),
                             key=lambda x: (x[0], x[1])))
    remaining = sorted(candidates, key=lambda x: (x[0], x[1]))
    seen = set()
    for _, label, plan in preferred + remaining:
        digest = canonical_hash(plan)
        if digest not in seen:
            seen.add(digest)
            yield label, plan


def legal_task_order(graph, plan, dependencies):
    """Reject task-order cycles before an expensive official simulation."""
    try:
        view = derive_multicore_plan(graph, plan)
        mapping = view["mapping"]
        tasks = set(view["nodes_by_subgraph"])
        successors = {task: set() for task in tasks}
        indegree = {task: 0 for task in tasks}

        def edge(source, target):
            if source != target and target not in successors[source]:
                successors[source].add(target)
                indegree[target] += 1

        for dst, links in dependencies.items():
            for src in links:
                edge(mapping[src], mapping[dst])
        for schedule in plan["core_schedules"]:
            for source, target in zip(schedule, schedule[1:]):
                edge(source, target)
        ready = [task for task, count in indegree.items() if count == 0]
        heapq.heapify(ready)
        visited = 0
        while ready:
            source = heapq.heappop(ready)
            visited += 1
            for target in successors[source]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    heapq.heappush(ready, target)
        return visited == len(tasks)
    except (KeyError, TypeError, ValueError, RuntimeError):
        return False


def optimize_case(seed_run, case, cores, max_evaluations, case_seconds, strategy="default"):
    old_job, old_plan, baseline = seed_case(seed_run, case, cores)
    graph = read_json(DATA / f"{case}.json")
    config = settings()
    _, _, dependencies = graph_view(graph)
    start = time.perf_counter()
    incumbent = old_job["selected"]
    selected_plan = old_plan
    selected_source = "seed_" + old_job["selected_source"]
    evaluations = [{"label": selected_source, **incumbent}]
    errors = []
    seen = {canonical_hash(old_plan)}
    calls = 0
    screened = 0

    def evaluate_candidate(label, plan):
        nonlocal calls, screened, incumbent, selected_plan, selected_source
        digest = canonical_hash(plan)
        if digest in seen:
            return
        seen.add(digest)
        if not legal_task_order(graph, plan, dependencies):
            screened += 1
            return
        if calls >= max_evaluations or time.perf_counter() - start >= case_seconds:
            return
        calls += 1
        metrics = bounded_attempt(graph, plan, config, label, start + case_seconds,
                                  evaluations, errors)
        if metrics and score(metrics) < score(incumbent):
            incumbent, selected_plan, selected_source = metrics, plan, label

    speedup = baseline["makespan"] / incumbent["makespan"]
    active = sum(bool(row) for row in old_plan["core_schedules"])
    if strategy in {"affinity", "chains"}:
        generator = affinity_candidates if strategy == "affinity" else chain_candidates
        for label, plan in generator(graph, cores, config):
            if calls >= max_evaluations or time.perf_counter() - start >= case_seconds:
                break
            evaluate_candidate(label, plan)
    if strategy == "default" and (active < cores or speedup <= 1.2):
        for label, plan in component_candidates(graph, cores):
            if calls >= max_evaluations or time.perf_counter() - start >= case_seconds:
                break
            evaluate_candidate(label, plan)
        if calls < max_evaluations and time.perf_counter() - start < case_seconds:
            evaluate_candidate("one_task", one_task_plan(graph, cores))
        if calls < max_evaluations and time.perf_counter() - start < case_seconds:
            for label, plan in coarse_candidates(graph, cores, config):
                if calls >= max_evaluations or time.perf_counter() - start >= case_seconds:
                    break
                evaluate_candidate(label, plan)
    # Refine the current winner. Fine-grained incumbent plans remain eligible.
    if strategy == "default" and calls < max_evaluations and time.perf_counter() - start < case_seconds:
        for label, plan in a_neighbors(graph, selected_plan, limit=40):
            if calls >= max_evaluations or time.perf_counter() - start >= case_seconds:
                break
            evaluate_candidate("refine_" + label, plan)

    elapsed = round(time.perf_counter() - start, 3)
    job = {"case": case, "cores": cores, "scene": "A", "status": "ok",
           "selected_source": selected_source, "selected": incumbent,
           "selected_plan_hash": canonical_hash(selected_plan),
           "paired": None, "b_plan_hash": None,
           "total_seconds": elapsed, "official_evaluations": calls,
           "budget": max_evaluations, "time_budget_seconds": case_seconds,
           "screened_invalid_candidates": screened,
           "seed_makespan": old_job["selected"]["makespan"],
           "seed_job_seconds": old_job["total_seconds"],
           "makespan_saved": old_job["selected"]["makespan"] - incumbent["makespan"],
           "evaluations": evaluations, "errors": errors}
    return job, selected_plan, baseline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-run", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", required=True)
    parser.add_argument("--cores", type=int, default=5, choices=(2, 3, 4, 5))
    parser.add_argument("--scene-a-only", action="store_true")
    parser.add_argument("--max-evaluations", type=int, default=8)
    parser.add_argument("--case-seconds", type=int, default=600)
    parser.add_argument("--strategy", choices=("default", "affinity", "chains"), default="default")
    parser.add_argument("--output-root", type=Path, default=ROOT / "targeted_runs")
    parser.add_argument("--run", type=Path, help="resume a targeted run")
    args = parser.parse_args()
    if args.max_evaluations < 1 or args.case_seconds < 1:
        parser.error("evaluation and time limits must be positive")
    cases = sorted(set(args.cases))
    if any(not (DATA / f"{case}.json").is_file() for case in cases):
        parser.error("unknown case")
    seed_run = args.seed_run.resolve()
    identity = {"cases": cases, "cores": [args.cores], "scenes": ["A"],
                "seed_run": str(seed_run), "max_evaluations": args.max_evaluations,
                "case_seconds": args.case_seconds, "strategy": args.strategy,
                "config_sha256": file_hash(DATA / "config.txt"),
                "input_hashes": input_hashes(cases),
                "source_hashes": source_hashes()}
    if args.run:
        run = args.run.resolve()
        manifest = read_json(run / "manifest.json")
        if any(manifest.get(key) != value for key, value in identity.items()):
            parser.error("resume mismatch: inputs, settings or source changed")
    else:
        run = args.output_root / (datetime.now().strftime("%Y%m%d_%H%M%S_")
                                  + secrets.token_hex(3))
        run.mkdir(parents=True, exist_ok=False)
        manifest = {"algorithm": "npu_5_targeted_a", "created_at":
                    datetime.now().astimezone().isoformat(), **identity}
    manifest["status"] = "running"
    atomic_json(run / "manifest.json", manifest)
    print(f"RUN={run.resolve()}", flush=True)
    for case in cases:
        job_path, plan_path = job_paths(run, case, args.cores, "A")
        if job_path.is_file() and plan_path.is_file():
            job = read_json(job_path)
            if job.get("status") == "ok" and job.get("selected_plan_hash") == canonical_hash(read_json(plan_path)):
                print(f"CACHED {case}", flush=True)
                continue
        job, plan, baseline = optimize_case(seed_run, case, args.cores,
                                            args.max_evaluations, args.case_seconds, args.strategy)
        atomic_json(run / "baseline" / "jobs" / f"{case}.json", baseline)
        atomic_json(plan_path, plan)
        atomic_json(job_path, job)
        print(f"DONE {case} makespan={job['selected']['makespan']} "
              f"saved={job['makespan_saved']} calls={job['official_evaluations']} "
              f"seconds={job['total_seconds']}", flush=True)
    failures, _ = summarize(run, cases, [args.cores], ["A"])
    audit = validate(run) if not failures else {"status": "error", "issues": failures}
    atomic_json(run / "reports" / "validation.json", audit)
    manifest["status"] = "complete" if audit["status"] == "ok" else "incomplete"
    manifest["problem_1_complete"] = False  # Deliberately not a 100-case run.
    manifest["finished_at"] = datetime.now().astimezone().isoformat()
    atomic_json(run / "manifest.json", manifest)
    print(f"STATUS={manifest['status']}", flush=True)


if __name__ == "__main__":
    main()
