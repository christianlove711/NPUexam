"""Thin adapters to the unmodified contest evaluators in sibling code/."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
CODE = PROJECT / "code"
DATA = PROJECT / "data"
if not (CODE / "evaluation_validation.py").is_file():
    raise RuntimeError(f"Official code missing: {CODE}")
if str(CODE) not in sys.path:
    sys.path.insert(0, str(CODE))

from evaluation_validation import read_evaluation_config, validate_task_order, validate_execution  # noqa: E402
from multicore_cut_evaluate_problem_1 import evaluate_scene_a, read_scene_a_config  # noqa: E402
from multicore_cut_evaluate_problem_2 import evaluate_scene_b, read_scene_b_config  # noqa: E402
from multicore_cut_evaluate_problem_3 import evaluate_problem_3, read_cache_config  # noqa: E402
from multicore_cut_evaluate_problem_2 import _build_scene_b_tasks as build_b_tasks  # noqa: E402
from multicore_cut_evaluate_problem_3 import _build_scene_b_tasks as build_l2_tasks  # noqa: E402
from singlecore_evaluate import evaluate_singlecore  # noqa: E402
from stub_multicore_cut_and_schedule import derive_multicore_plan  # noqa: E402


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def settings():
    path = str(DATA / "config.txt")
    common = read_evaluation_config(path)
    a = read_scene_a_config(path)
    b = read_scene_b_config(path)
    return {"capacity": common["capacity"], "bandwidth": common["bandwidth"],
            "a_same_wait": a["task_same_core_wait_cycles"],
            "a_cross_wait": a["task_cross_core_wait_cycles"],
            "b_cross_wait": b["cross_core_copy_delay_cycles"],
            "cache": read_cache_config(path)}


def validate_candidate_structure(graph, plan, scene, config=None):
    """Run official checks through the execution DAG before event simulation.

    B/L2 task construction is substantial work and is repeated by the full
    evaluator for accepted candidates. Track its cost separately in the search.
    """
    view = derive_multicore_plan(graph, plan)
    if scene == "A":
        # Scene A adds serial task-order edges on each core. This DAG check is
        # cheap and catches cycles before constructing and simulating tasks.
        validate_task_order(view)
    elif config is not None and scene in {"B", "L2"}:
        builder = build_b_tasks if scene == "B" else build_l2_tasks
        tasks, cross_links, *_ = builder(
            graph, plan, config["bandwidth"], dict(config["capacity"]))
        validate_execution(tasks, cross_links)
    return view


def evaluate(graph, plan, scene, config):
    if set(plan) != {"node_to_subgraph", "core_schedules"}:
        raise ValueError("Plan must have exactly the two official fields")
    derive_multicore_plan(graph, plan)
    common = {"capacity": config["capacity"], "bandwidth": config["bandwidth"]}
    if scene == "A":
        result = evaluate_scene_a(graph, plan, **common,
                                  cross_core_wait=config["a_cross_wait"],
                                  same_core_wait=config["a_same_wait"])
    elif scene == "B":
        result = evaluate_scene_b(graph, plan, **common,
                                  cross_core_copy_delay=config["b_cross_wait"])
    elif scene == "L2":
        result = evaluate_problem_3(graph, plan, **common,
                                    cross_core_copy_delay=config["b_cross_wait"],
                                    **config["cache"])
    else:
        raise ValueError(scene)
    data = result["data_movement_bytes"]
    compact = {"makespan": result["makespan"],
               "added_copy_bytes": data["added_copy_bytes"],
               "scheduled_copy_bytes": data["scheduled_copy_bytes"],
               "original_graph_copy_bytes": data["original_graph_copy_bytes"],
               "memory_peak_by_core": result.get("memory_peak_by_core", {})}
    for field in ('partition_added_copy_bytes', 'spill_added_copy_bytes'):
        if field in data:
            compact[field] = data[field]
    # Keep a small summary of the official timeline for guiding the next
    # neighborhood. These values are diagnostics, never substitutes for score().
    core_end, pipe_busy, subgraph_end = [], [], {}
    for core in result.get("per_core_timeline", []):
        end = 0
        busy = {}
        for op in core.get("ops", []):
            end = max(end, op["end"])
            pipe = op["pipe"]
            busy[pipe] = busy.get(pipe, 0) + op["duration"]
            sg = op.get("subgraph_id")
            if sg is not None:
                subgraph_end[sg] = max(subgraph_end.get(sg, 0), op["end"])
        core_end.append(end)
        pipe_busy.append(busy)
    compact["diagnostics"] = {
        "core_end": core_end,
        "pipe_busy": pipe_busy,
        "late_subgraphs": [sg for sg, _ in sorted(
            subgraph_end.items(), key=lambda item: (-item[1], item[0]))[:24]],
    }
    if scene == "L2":
        stats = result["cache_stats"]
        compact.update(cache_hit_rate=stats["hit_rate"],
                       cache_hit_bytes=stats["hit_bytes"],
                       cache_miss_bytes=stats["miss_bytes"])
        issued, completed = set(), set()
        miss_reasons = {'first_read_bytes': 0, 'unfinished_first_read_bytes': 0,
                        'post_insert_miss_bytes': 0, 'oversized_bytes': 0}
        for event in result.get('cache_events', []):
            tid, size = event.get('tensor_id'), event.get('size_bytes', 0)
            if event['event'] == 'insert':
                completed.add(tid)
            elif event['event'] == 'miss':
                reason = ('oversized_bytes' if size > config['cache']['cache_capacity_bytes'] else
                          'post_insert_miss_bytes' if tid in completed else
                          'unfinished_first_read_bytes' if tid in issued else 'first_read_bytes')
                miss_reasons[reason] += size
                issued.add(tid)
        compact['diagnostics']['cache_miss_reasons'] = miss_reasons
    return compact


def baseline(graph, config):
    result = evaluate_singlecore(graph, bandwidth=config["bandwidth"],
                                 capacity=config["capacity"],
                                 cross_core_wait=config["a_cross_wait"],
                                 same_core_wait=config["a_same_wait"])
    return {"makespan": result["makespan"],
            "added_copy_bytes": result["data_movement_bytes"]["added_copy_bytes"]}


def score(metrics):
    return metrics["makespan"], metrics["added_copy_bytes"]


def attempt(graph, plan, scene, config, label, evaluations, errors):
    try:
        started = time.perf_counter()
        metrics = evaluate(graph, plan, scene, config)
        metrics["evaluation_seconds"] = round(time.perf_counter() - started, 3)
        evaluations.append({"label": label, **metrics})
        return metrics
    except Exception as exc:
        errors.append({"label": label, "error": f"{type(exc).__name__}: {exc}"})
        return None
