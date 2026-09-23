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

from evaluation_validation import read_evaluation_config  # noqa: E402
from multicore_cut_evaluate_problem_1 import evaluate_scene_a, read_scene_a_config  # noqa: E402
from multicore_cut_evaluate_problem_2 import evaluate_scene_b, read_scene_b_config  # noqa: E402
from multicore_cut_evaluate_problem_3 import evaluate_problem_3, read_cache_config  # noqa: E402
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
    if scene == "L2":
        stats = result["cache_stats"]
        compact.update(cache_hit_rate=stats["hit_rate"],
                       cache_hit_bytes=stats["hit_bytes"],
                       cache_miss_bytes=stats["miss_bytes"])
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
