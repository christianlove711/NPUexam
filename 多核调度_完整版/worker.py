"""单组配置的隔离评估进程，由 run_all.py 启动并控制超时。

负责官方评估、第一版回退、题三同方案配对、有限邻域搜索和结果 JSON。
Makespan 单位为模拟 cycles，evaluation_seconds 是电脑上的现实耗时。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import traceback
from collections import defaultdict
from pathlib import Path

from solver_v2 import (ATTACHMENT, _graph_view, generate_candidates,
                       legacy_plan)
from evaluation_validation import read_evaluation_config
from multicore_cut_evaluate_problem_1 import evaluate_scene_a, read_scene_a_config
from multicore_cut_evaluate_problem_2 import evaluate_scene_b, read_scene_b_config
from multicore_cut_evaluate_problem_3 import evaluate_problem_3, read_cache_config
from singlecore_evaluate import build_singlecore_plan, evaluate_singlecore
from stub_multicore_cut_and_schedule import derive_multicore_plan


def atomic_json(path, value):
    """用临时文件加替换写入一个 JSON；多个结果文件之间不构成事务。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def settings():
    """仅从附件 data/config.txt 读取官方容量、带宽、等待和 Cache 参数。"""
    path = ATTACHMENT / "data" / "config.txt"
    common = read_evaluation_config(str(path))
    scene_a = read_scene_a_config(str(path))
    scene_b = read_scene_b_config(str(path))
    cache = read_cache_config(str(path))
    return {
        "capacity": common["capacity"], "bandwidth": common["bandwidth"],
        "a_same_wait": scene_a["task_same_core_wait_cycles"],
        "a_cross_wait": scene_a["task_cross_core_wait_cycles"],
        "b_cross_wait": scene_b["cross_core_copy_delay_cycles"],
        "cache": cache,
    }


def official_evaluate(graph, plan, scene, config):
    """调用对应场景的官方模拟器并保留精简指标，不保存完整操作 Trace。

added_copy_bytes 是官方逻辑 COPY 统计；不能当作扣除 L2 命中后的物理 DDR 流量。
    """
    common = {"capacity": config["capacity"], "bandwidth": config["bandwidth"]}
    if scene == "A":
        result = evaluate_scene_a(
            graph, plan, **common,
            cross_core_wait=config["a_cross_wait"],
            same_core_wait=config["a_same_wait"])
    elif scene == "B":
        result = evaluate_scene_b(
            graph, plan, **common,
            cross_core_copy_delay=config["b_cross_wait"])
    else:
        result = evaluate_problem_3(
            graph, plan, **common,
            cross_core_copy_delay=config["b_cross_wait"], **config["cache"])
    data = result["data_movement_bytes"]
    compact = {
        "makespan": result["makespan"],
        "added_copy_bytes": data["added_copy_bytes"],
        "scheduled_copy_bytes": data["scheduled_copy_bytes"],
        "original_graph_copy_bytes": data["original_graph_copy_bytes"],
        "memory_peak_by_core": result.get("memory_peak_by_core", {}),
    }
    if scene == "L2":
        stats = result["cache_stats"]
        compact.update(cache_hit_rate=stats["hit_rate"],
                       cache_hit_bytes=stats["hit_bytes"],
                       cache_miss_bytes=stats["miss_bytes"])
    return compact


def score(metrics):
    """字典序目标：先最小化 Makespan，时间相同再最小化额外 COPY 字节。"""
    return metrics["makespan"], metrics["added_copy_bytes"]


def attempt(graph, plan, scene, config, label, evaluations, errors):
    """校验并评估一个候选；成功记录指标，失败记录原因并返回 None。

evaluation_seconds 从格式/依赖校验后开始计时，不包含此前校验时间。
    """
    try:
        derive_multicore_plan(graph, plan)
        began = time.perf_counter()
        metrics = official_evaluate(graph, plan, scene, config)
        metrics["evaluation_seconds"] = round(time.perf_counter() - began, 3)
        evaluations.append({"label": label, **metrics})
        return metrics
    except Exception as exc:
        errors.append({"label": label, "error": f"{type(exc).__name__}: {exc}"})
        return None


def _problem_path(run, scene):
    return Path(run) / {"A": "problem_1", "B": "problem_2", "L2": "problem_3"}[scene]


def run_job(run, case, cores, scene):
    """完成一组必需评估并保存最优方案和候选记录。

L2 先将当前 B 方案分别评估为无/有 Cache，随后单独选择 L2 最终方案。
配对结果用于解释 Cache 收益，selected 用于报告题三最终成绩。
    """
    run = Path(run)
    graph = read_json(ATTACHMENT / "data" / f"{case}.json")
    config = settings()
    folder = _problem_path(run, scene)
    evaluations, errors = [], []
    began = time.perf_counter()
    candidates = []
    paired = None
    b_plan_hash = None
    if scene in ("A", "B"):
        try:
            candidates.append(("v1", legacy_plan(graph, cores, scene)))
        except Exception as exc:
            errors.append({"label": "v1_generation",
                           "error": f"{type(exc).__name__}: {exc}"})
        try:
            candidates += [(f"v2_{label}", plan) for label, plan, _
                           in generate_candidates(graph, cores, scene, config)]
        except Exception as exc:
            errors.append({"label": "v2_generation",
                           "error": f"{type(exc).__name__}: {exc}"})
    else:
        b_plan_path = run / "problem_2" / "plans" / f"{case}_{cores}core.json"
        b_plan = read_json(b_plan_path)
        b_plan_hash = file_hash(b_plan_path)
        no_cache = attempt(graph, b_plan, "B", config,
                           "paired_no_l2", evaluations, errors)
        with_cache = attempt(graph, b_plan, "L2", config,
                             "paired_with_l2", evaluations, errors)
        if no_cache and with_cache:
            paired = {"no_l2": no_cache, "with_l2": with_cache,
                      "cache_speedup": (no_cache["makespan"] /
                                        max(with_cache["makespan"], 1))}
        candidates.append(("paired_b_plan", b_plan))
        try:
            candidates.append(("v1_l2", legacy_plan(graph, cores, "L2")))
        except Exception as exc:
            errors.append({"label": "v1_l2_generation",
                           "error": f"{type(exc).__name__}: {exc}"})
        try:
            candidates += [(f"v2_{label}", plan) for label, plan, _
                           in generate_candidates(graph, cores, "L2", config)]
        except Exception as exc:
            errors.append({"label": "v2_l2_generation",
                           "error": f"{type(exc).__name__}: {exc}"})
    generation_seconds = round(time.perf_counter() - began, 3)
    best = None
    v1_metrics = None
    for label, plan in candidates:
        if scene == "L2" and label == "paired_b_plan" and paired:
            metrics = paired["with_l2"]
        else:
            metrics = attempt(graph, plan, scene, config, label,
                              evaluations, errors)
        if label.startswith("v1"):
            v1_metrics = metrics
        if metrics and (best is None or score(metrics) < score(best[2])):
            best = label, plan, metrics
    if best:
        atomic_json(folder / "plans" / f"{case}_{cores}core.json", best[1])
    row = {
        "case": case, "cores": cores, "scene": scene,
        "status": "ok" if best and (scene != "L2" or paired) else "error",
        "selected_source": best[0] if best else None,
        "selected": best[2] if best else None,
        "v1": v1_metrics,
        "paired": paired,
        "b_plan_sha256": b_plan_hash,
        "generation_seconds": generation_seconds,
        "total_seconds": round(time.perf_counter() - began, 3),
        "evaluations": evaluations, "errors": errors,
    }
    atomic_json(folder / "jobs" / f"{case}_{cores}core.json", row)
    return row


def run_singlecore(run, case):
    """计算官方单核基准，再用同一整图单核方案做 B/L2 配对。"""
    graph = read_json(ATTACHMENT / "data" / f"{case}.json")
    config = settings()
    began = time.perf_counter()
    result = evaluate_singlecore(
        graph, bandwidth=config["bandwidth"], capacity=config["capacity"],
        cross_core_wait=config["a_cross_wait"],
        same_core_wait=config["a_same_wait"])
    one_plan = build_singlecore_plan(graph)
    no_l2 = official_evaluate(graph, one_plan, "B", config)
    with_l2 = official_evaluate(graph, one_plan, "L2", config)
    row = {"case": case, "status": "ok", "makespan": result["makespan"],
           "added_copy_bytes": result["data_movement_bytes"]["added_copy_bytes"],
           "paired_no_l2_makespan": no_l2["makespan"],
           "paired_with_l2_makespan": with_l2["makespan"],
           "paired_no_l2_added_copy_bytes": no_l2["added_copy_bytes"],
           "paired_with_l2_added_copy_bytes": with_l2["added_copy_bytes"],
           "paired_l2_speedup": no_l2["makespan"] / max(with_l2["makespan"], 1),
           "paired_cache_hit_rate": with_l2["cache_hit_rate"],
           "evaluation_seconds": round(time.perf_counter() - began, 3)}
    atomic_json(Path(run) / "baseline" / "jobs" / f"{case}.json", row)
    return row


def _neighbors(graph, plan):
    """从输入方案一次性构造至多五个合法的迁核、交换、合并、重排、拆分邻居。"""
    schedules = plan["core_schedules"]
    mapping = plan["node_to_subgraph"]
    core_sizes = [sum(1 for sg in mapping.values() if sg in row)
                  for row in schedules]
    large_core = max(range(len(schedules)), key=lambda i: core_sizes[i])
    small_core = min(range(len(schedules)), key=lambda i: core_sizes[i])
    proposals = []

    def add(label, candidate):
        try:
            derive_multicore_plan(graph, candidate)
        except Exception:
            return
        proposals.append((label, candidate))

    if large_core != small_core and schedules[large_core]:
        sg = schedules[large_core][-1]
        for position in range(len(schedules[small_core]) + 1):
            rows = [list(row) for row in schedules]
            rows[large_core].remove(sg)
            rows[small_core].insert(position, sg)
            candidate = {"node_to_subgraph": dict(mapping), "core_schedules": rows}
            before = len(proposals)
            add("migrate", candidate)
            if len(proposals) > before:
                break
    if large_core != small_core and schedules[large_core] and schedules[small_core]:
        rows = [list(row) for row in schedules]
        rows[large_core][-1], rows[small_core][-1] = (
            rows[small_core][-1], rows[large_core][-1])
        add("swap", {"node_to_subgraph": dict(mapping), "core_schedules": rows})
    for core, row in enumerate(schedules):
        if len(row) < 2:
            continue
        a, b = row[-2:]
        rows = [list(items) for items in schedules]
        rows[core].remove(b)
        merged = {node: (a if sg == b else sg) for node, sg in mapping.items()}
        add("merge", {"node_to_subgraph": merged, "core_schedules": rows})
        rows = [list(items) for items in schedules]
        rows[core][-2:] = [b, a]
        add("reorder", {"node_to_subgraph": dict(mapping),
                        "core_schedules": rows})
        break
    _, topo, _ = _graph_view(graph)
    by_sg = defaultdict(list)
    for node in topo:
        by_sg[mapping[str(node)]].append(node)
    if by_sg:
        sg = max(by_sg, key=lambda key: len(by_sg[key]))
        nodes = by_sg[sg]
        if len(nodes) >= 4:
            new_sg = max(by_sg) + 1
            split = dict(mapping)
            for node in nodes[len(nodes) // 2:]:
                split[str(node)] = new_sg
            rows = [list(items) for items in schedules]
            for row in rows:
                if sg in row:
                    row.insert(row.index(sg) + 1, new_sg)
                    break
            add("split", {"node_to_subgraph": split, "core_schedules": rows})
    return proposals[:5]


def search_job(run, case, cores, scene):
    """评估一批邻居并保留改进；不会围绕新优解继续生成下一轮邻居。

搜索记录单独存放；初始 job 中 generation_seconds/total_seconds 不累计搜索耗时。
    """
    run = Path(run)
    folder = _problem_path(run, scene)
    job_path = folder / "jobs" / f"{case}_{cores}core.json"
    row = read_json(job_path)
    if row["status"] != "ok":
        raise RuntimeError("cannot search an unsuccessful required job")
    graph = read_json(ATTACHMENT / "data" / f"{case}.json")
    plan_path = folder / "plans" / f"{case}_{cores}core.json"
    plan = read_json(plan_path)
    config = settings()
    current = row["selected"]
    original = current
    search_evaluations = []
    search_errors = []
    for label, candidate in _neighbors(graph, plan):
        metrics = attempt(graph, candidate, scene, config, label,
                          search_evaluations, search_errors)
        if metrics and score(metrics) < score(current):
            plan, current = candidate, metrics
            row["selected_source"] = f"search_{label}"
    if score(current) < score(row["selected"]):
        atomic_json(plan_path, plan)
        row["selected"] = current
        atomic_json(job_path, row)
    record = {"case": case, "cores": cores, "scene": scene,
              "evaluations": search_evaluations, "errors": search_errors,
              "improved": score(current) < score(original),
              "b_plan_sha256": (file_hash(run / "problem_2" / "plans"
                                          / f"{case}_{cores}core.json")
                                   if scene == "L2" else None)}
    atomic_json(folder / "search_jobs" / f"{case}_{cores}core.json", record)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--case", required=True)
    parser.add_argument("--cores", type=int)
    parser.add_argument("--scene", choices=("A", "B", "L2"))
    parser.add_argument("--phase", choices=("singlecore", "required", "search"),
                        required=True)
    args = parser.parse_args()
    try:
        if args.phase == "singlecore":
            result = run_singlecore(args.run, args.case)
        elif args.phase == "search":
            result = search_job(args.run, args.case, args.cores, args.scene)
        else:
            result = run_job(args.run, args.case, args.cores, args.scene)
        print(json.dumps({"status": result.get("status", "ok"),
                          "case": args.case, "cores": args.cores,
                          "scene": args.scene}, ensure_ascii=False))
        return 0 if result.get("status", "ok") == "ok" else 1
    except Exception:
        print(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
