"""Budgeted, multi-start search guided by official timeline diagnostics.

Experimental module. The official evaluator still decides legality and score.
"""
from __future__ import annotations

import copy
import math
import time
from collections import defaultdict

from official import attempt, canonical_hash, derive_multicore_plan, validate_candidate_structure, score
from solver import initial_candidates, neighbors, idblock_candidates, refinement_sizes, legal_insertions
from refine import structural_candidates, advanced_neighbors, partition_variants
from itertools import zip_longest


def guided_neighbors(graph, plan, scene, diagnostics):
    """Interleave move types and prioritize late, busy parts of the real schedule."""
    base = list(neighbors(graph, plan, scene, limit=128))
    core_end = diagnostics.get("core_end", [])
    late = diagnostics.get("late_subgraphs", [])
    late_rank = {sg: rank for rank, sg in enumerate(late)}
    groups = defaultdict(list)
    splitmove_sources = 0

    def put(label, candidate):
        kind = label.split("_", 1)[0]
        if kind not in {"move", "split", "splitmove", "merge", "swap"}:
            return
        parts = label.split("_")
        try:
            sg = int(parts[1])
        except (IndexError, ValueError):
            sg = -1
        priority = late_rank.get(sg, 1000)
        if kind in {"move", "splitmove"} and sg >= 0:
            source = next((c for c, row in enumerate(plan["core_schedules"])
                           if sg in row), None)
            moving_sg = int(parts[2]) if kind == "splitmove" else sg
            target = next((c for c, row in enumerate(candidate["core_schedules"])
                           if moving_sg in row), None)
            if source is not None and target is not None and source != target:
                gap = ((core_end[target] if target < len(core_end) else 0) -
                       (core_end[source] if source < len(core_end) else 0))
            else:
                gap = 0
        else:
            gap = 0
        groups[kind].append(((priority, gap, label), label, candidate))

    for label, candidate in base:
        put(label, candidate)
        if not label.startswith("split_"):
            continue
        # A neutral split on the source core can unlock useful parallelism only
        # if the new piece is moved in the same neighborhood step.
        original_sg = int(label.split("_")[1])
        sg = max(candidate["node_to_subgraph"].values())
        source = next(c for c, row in enumerate(candidate["core_schedules"]) if sg in row)
        if scene != "A":
            if splitmove_sources >= 2:
                continue
            try:
                split_view = derive_multicore_plan(graph, candidate)
            except Exception:
                continue
        splitmove_sources += 1
        targets = sorted((c for c in range(len(candidate["core_schedules"])) if c != source),
                         key=lambda c: (core_end[c] if c < len(core_end) else 0, c))[:2]
        for target in targets:
            positions = (legal_insertions(split_view, sg, candidate["core_schedules"][target])
                         if scene != "A" else
                         tuple(dict.fromkeys((0, len(candidate["core_schedules"][target]) // 2,
                                              len(candidate["core_schedules"][target])))))
            for position in positions:
                combined = {"node_to_subgraph": candidate["node_to_subgraph"],
                            "core_schedules": copy.deepcopy(candidate["core_schedules"])}
                combined["core_schedules"][source].remove(sg)
                combined["core_schedules"][target].insert(position, sg)
                put(f"splitmove_{original_sg}_{sg}_{target}_{position}", combined)

    order = (("merge", "move", "splitmove", "swap", "split") if scene == "A" else
             ("move", "splitmove", "merge", "swap", "split"))
    for kind in groups:
        groups[kind].sort(key=lambda item: item[0])
    index = {kind: 0 for kind in order}
    while any(index[kind] < len(groups[kind]) for kind in order):
        for kind in order:
            if index[kind] < len(groups[kind]):
                _, label, candidate = groups[kind][index[kind]]
                index[kind] += 1
                yield label, candidate


def search(graph, cores, scene, config, budget, extras=(), preevaluated=()):
    evaluations, errors, leaders, seen = [], [], [], set()
    max_attempts = max(4 * budget, 160)
    counts = {"attempted_candidates": 0, "official_evaluations": 0,
              "static_rejected": 0, "official_valid": 0,
              "official_invalid": 0, "preflight_rejected": 0,
              "preflight_seconds": 0.0, "execution_preflight_calls": 0,
              "execution_cycle_failures": 0}

    def add(label, plan, metrics=None):
        nonlocal counts
        # Historical metrics are never trusted for selection; every distinct
        # seed is rescored with the current official evaluator.
        metrics = None
        digest = canonical_hash(plan)
        if digest in seen:
            return None
        if counts["attempted_candidates"] >= max_attempts:
            return None
        seen.add(digest)
        counts["attempted_candidates"] += 1
        if metrics is None:
            # Static rejects are recorded separately and do not consume an
            # official evaluator call. Every actual call, including a failed
            # call, does consume the official-evaluation budget.
            preflight_started = time.perf_counter()
            use_execution_preflight = (scene in {"B", "L2"} and
                                       counts["execution_cycle_failures"] >= 2)
            if use_execution_preflight:
                counts["execution_preflight_calls"] += 1
            try:
                validate_candidate_structure(
                    graph, plan, scene,
                    config if scene == "A" or use_execution_preflight else None)
            except Exception as exc:
                counts["static_rejected"] += 1
                counts["preflight_rejected"] += 1
                errors.append({"label": label, "error": f"{type(exc).__name__}: {exc}"})
                return None
            finally:
                counts["preflight_seconds"] += time.perf_counter() - preflight_started
            if counts["official_evaluations"] >= budget:
                return None
            counts["official_evaluations"] += 1
            metrics = attempt(graph, plan, scene, config, label, evaluations, errors)
            if metrics:
                evaluations[-1]["plan_hash"] = digest
                counts["official_valid"] += 1
            else:
                counts["official_invalid"] += 1
                if errors and "global execution" in errors[-1]["error"] and "cycle" in errors[-1]["error"]:
                    counts["execution_cycle_failures"] += 1
        if metrics:
            item = (score(metrics), label, plan, metrics)
            leaders.append(item)
            leaders.sort(key=lambda row: (row[0], row[1]))
            del leaders[8:]
            return item
        return None

    for label, plan, _metrics in preevaluated:
        add(label, plan)
    for label, plan in extras:
        add(label, plan)
    # Historical B may look worse than the A seed only because it spills.
    # Polish each seed before initial grids can evict it from the top-eight pool.
    counts['warm_start_polish_calls'] = 0
    if leaders and graph.get('tensors') and counts['official_evaluations'] < budget:
        before = counts['official_evaluations']
        polish_limit = min(budget, before + min(8, max(2, budget // 8)))
        queues = []
        for item in leaders[:4]:
            try:
                variants = list(partition_variants(graph, item[2], scene, config))
                priority = {'memory_order': 0, 'cache_order': 1, 'placement': 2, 'critical_order': 3}
                variants.sort(key=lambda pair: priority.get(pair[0], 4))
                queues.append([(f'polish_{item[1]}_{label}', candidate) for label,candidate in variants])
            except Exception as exc:
                errors.append({'label': 'seed_polish_generation', 'error': str(exc)})
        for row in zip_longest(*queues):
            for item in row:
                if item is None or counts['official_evaluations'] >= polish_limit:
                    continue
                add(*item)
            if counts['official_evaluations'] >= polish_limit:
                break
        counts['warm_start_polish_calls'] = counts['official_evaluations'] - before
    # Structural routes retain the fifth-version improvements in all scenes.
    if counts['official_evaluations'] < budget and graph.get('tensors'):
        try:
            for label, plan in structural_candidates(graph, cores, scene, config):
                if counts['official_evaluations'] >= min(budget, max(10, budget // 4)):
                    break
                add('structure_' + label, plan)
        except Exception as exc:
            errors.append({'label': 'structure_generation', 'error': str(exc)})
    # On large graphs, spend a deliberate first tranche on the coarse grid,
    # then refine only around the best official-scored coarse grain.
    operation_count = sum(op["op"] not in {"COPY_IN", "COPY_OUT"}
                         for op in graph["ops"])
    if operation_count > 5000:
        coarse = list(idblock_candidates(graph, cores, (4, 16, 48, 192, 768)))
        coarse_scored = []
        for label, plan in coarse:
            if counts["official_evaluations"] >= budget or counts["attempted_candidates"] >= max_attempts:
                break
            item = add(f"coarse_{label}", plan)
            if item:
                coarse_scored.append(item)
        if coarse_scored and counts["official_evaluations"] < budget:
            winner = min(coarse_scored, key=lambda item: (item[0], item[1]))
            grain = int(winner[1].rsplit("_", 1)[1])
            refined = list(idblock_candidates(graph, cores, refinement_sizes(grain)))
            for label, plan in refined:
                if counts["official_evaluations"] >= budget or counts["attempted_candidates"] >= max_attempts:
                    break
                add(f"refine_{label}", plan)
    try:
        for label, plan in initial_candidates(graph, cores, scene, config):
            if counts["official_evaluations"] >= budget or counts["attempted_candidates"] >= max_attempts:
                break
            add(label, plan)
    except Exception as exc:
        errors.append({"label": "initial_generation", "error": f"{type(exc).__name__}: {exc}"})
    if not leaders:
        raise RuntimeError(f"No official-valid {scene} plan: {errors[:10]}")

    # Each start keeps its own current score. One start may improve locally
    # before surpassing the other start's global best.
    starts = [{"current": item, "stagnation": 0, "active": True}
              for item in leaders[:2]]
    started_hashes = {canonical_hash(state["current"][2]) for state in starts}
    rounds = 0
    counts["search_restarts"] = 0
    while (counts["official_evaluations"] < budget and
           counts["attempted_candidates"] < max_attempts):
        if not any(state["active"] for state in starts):
            next_start = next((item for item in leaders
                               if canonical_hash(item[2]) not in started_hashes), None)
            if next_start is None:
                break
            started_hashes.add(canonical_hash(next_start[2]))
            starts = [{"current": next_start, "stagnation": 0, "active": True}]
            counts["search_restarts"] += 1
        rounds += 1
        for state in starts:
            if counts["official_evaluations"] >= budget or not state["active"]:
                continue
            current = state["current"]
            remaining_starts = sum(item["active"] for item in starts)
            quota = min(16, max(4, math.ceil((budget - counts["official_evaluations"]) / max(remaining_starts * 2, 1))))
            tried = 0
            best_local = current
            mild_perturbation = None
            try:
                diagnostics = current[3].get('diagnostics', {})
                old_moves = guided_neighbors(graph, current[2], scene, diagnostics)
                advanced = (list(advanced_neighbors(graph, current[2], scene, config, diagnostics))
                            if graph.get('tensors') else [])
                families = defaultdict(list)
                for item in advanced:
                    families[item[0].split('_')[0]].append(item)
                advanced = [item for row in zip_longest(*families.values())
                            for item in row if item is not None]
                offset = ((rounds - 1) * 6) % max(1, len(advanced))
                new_moves = iter(advanced[offset:] + advanced[:offset])
                # Alternate families so a long merge/move list cannot starve
                # partition rescheduling, weighted splits or boundary transfers.
                moves = (item for pair in zip_longest(new_moves, old_moves)
                         for item in pair if item is not None)
                for move_label, candidate in moves:
                    if (tried >= quota or counts["official_evaluations"] >= budget or
                            counts["attempted_candidates"] >= max_attempts):
                        break
                    before_calls = counts["official_evaluations"]
                    item = add(f"round{rounds}_{current[1]}_{move_label}", candidate)
                    if counts["official_evaluations"] == before_calls:
                        continue
                    tried += 1
                    if item and item[0] < best_local[0]:
                        best_local = item
                    elif item and item[0][0] <= current[0][0] * 1.005:
                        if mild_perturbation is None or item[0] < mild_perturbation[0]:
                            mild_perturbation = item
            except Exception as exc:
                errors.append({"label": f"round{rounds}_{current[1]}",
                               "error": f"{type(exc).__name__}: {exc}"})
            if best_local[0] < current[0]:
                state["current"] = best_local
                state["stagnation"] = 0
            elif state["stagnation"] == 0 and mild_perturbation:
                state["current"] = mild_perturbation
                state["stagnation"] = 1
            else:
                state["active"] = False
    leaders.sort(key=lambda item: (item[0], item[1]))
    counts["search_rounds"] = rounds
    counts["unused_budget"] = budget - counts["official_evaluations"]
    counts["max_attempts"] = max_attempts
    return leaders[0], leaders[:4], evaluations, errors, counts
