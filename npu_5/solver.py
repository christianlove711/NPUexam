"""Deterministic candidate and neighborhood generation for all three scenes.

All estimates here only decide which plans to try. The official evaluators
alone determine feasibility, Makespan, movement, and Cache statistics.
"""
from __future__ import annotations

import copy
from collections import defaultdict

from partition import generate_candidates
from fixed import _minimum_id_topological_order, _one_plan
from official import derive_multicore_plan
from graph import graph_view
from stub_multicore_cut_and_schedule import (
    _build_op_adjacency, _contract_excluded_copy_nodes,
)


def idblock_candidates(graph, cores, sizes):
    ops = {op["id"]: op for op in graph["ops"]
           if op["op"] not in {"COPY_IN", "COPY_OUT"}}
    _, full = _build_op_adjacency(graph)
    preds, succs = _contract_excluded_copy_nodes(sorted(ops), full)
    order = _minimum_id_topological_order(ops, preds, succs)
    for size in dict.fromkeys(min(size, max(1, len(order))) for size in sizes):
        yield f"fixed_{size}", _one_plan(graph, cores, size, ops, order, succs)


def component_candidates(graph, cores):
    """Place independent operation components on different cores for scene A."""
    ops, order, preds = graph_view(graph)
    adjacency = {node: set() for node in order}
    for dst, links in preds.items():
        for src in links:
            adjacency[src].add(dst)
            adjacency[dst].add(src)
    seen = set()
    components = []
    for node in order:
        if node in seen:
            continue
        pending = [node]
        seen.add(node)
        component = []
        while pending:
            current = pending.pop()
            component.append(current)
            for neighbor in sorted(adjacency[current]):
                if neighbor not in seen:
                    seen.add(neighbor)
                    pending.append(neighbor)
        components.append(component)
    if len(components) < 2:
        return

    by_pipe = []
    for component in components:
        pipes = {}
        for node in component:
            op = ops[node]
            pipes[op["pipe"]] = pipes.get(op["pipe"], 0) + op["cycles"]
        by_pipe.append(pipes)
    ordering = sorted(range(len(components)),
                      key=lambda i: (-max(by_pipe[i].values()),
                                     -sum(by_pipe[i].values()), min(components[i])))
    for mode in ("total", "pipe"):
        mapping = {}
        schedules = [[] for _ in range(cores)]
        loads = [{} for _ in range(cores)]
        for subgraph, index in enumerate(ordering):
            pipes = by_pipe[index]

            def projected(core):
                combined = {pipe: loads[core].get(pipe, 0) + pipes.get(pipe, 0)
                            for pipe in set(loads[core]) | set(pipes)}
                return max(combined.values(), default=0)

            if mode == "pipe":
                core = min(range(cores), key=lambda c: (projected(c),
                           sum(loads[c].values()), c))
            else:
                core = min(range(cores), key=lambda c: (
                    max(loads[c].values(), default=0), len(schedules[c]), c))
            schedules[core].append(subgraph)
            for pipe, work in pipes.items():
                loads[core][pipe] = loads[core].get(pipe, 0) + work
            for node in components[index]:
                mapping[str(node)] = subgraph
        yield f"components_{mode}", {"node_to_subgraph": mapping,
                                      "core_schedules": schedules}


def initial_candidates(graph, cores, scene, config):
    """Explore both fixed granularity and communication-aware cuts.

    The fourth-version search used only one node-ID topological order. The
    existing v2 generator supplies three structurally different topological
    priorities and communication-aware cut positions; here all of its valid
    candidates, rather than only its top two proxy scores, receive a chance at
    official evaluation.
    """
    count = sum(op["op"] not in {"COPY_IN", "COPY_OUT"} for op in graph["ops"])
    sizes = ((1, 2, 4, 8, 16, 24, 48, 96, 192, 384, 768)
             if count <= 5000 else (1, 4, 16, 48, 192, 384, 768, 1536))
    yield from idblock_candidates(graph, cores, sizes)
    for label, plan, _ in generate_candidates(graph, cores, scene, config,
                                               limit=12):
        yield f"adaptive_{label}", plan


def _op_order(graph):
    ops = {op["id"]: op for op in graph["ops"]
           if op["op"] not in {"COPY_IN", "COPY_OUT"}}
    _, full_succs = _build_op_adjacency(graph)
    preds, succs = _contract_excluded_copy_nodes(sorted(ops), full_succs)
    return ops, _minimum_id_topological_order(ops, preds, succs)


def neighbors(graph, plan, scene, limit=32):
    """Yield distinct local changes, leaving legality to the official parser.

    A benefits from fewer Task boundaries; B/L2 can benefit from moving small
    subgraphs between cores. The same mutation set is tested under the real
    scene evaluator, so its proxy workload is never reported as a score.
    """
    if scene not in {"A", "B", "L2"}:
        raise ValueError(scene)
    view = derive_multicore_plan(graph, plan)
    ops, order = _op_order(graph)
    mapping = plan["node_to_subgraph"]
    schedules = plan["core_schedules"]
    groups = view["nodes_by_subgraph"]
    by_pipe = defaultdict(lambda: defaultdict(int))
    for node, sg in view["mapping"].items():
        by_pipe[sg][ops[node]["pipe"]] += max(0, ops[node]["cycles"])
    work = {sg: max(pipes.values(), default=0) for sg, pipes in by_pipe.items()}
    loads = [sum(work[sg] for sg in row) for row in schedules]
    emitted = 0

    def candidate(label, new_mapping, new_schedules):
        return label, {"node_to_subgraph": new_mapping,
                       "core_schedules": new_schedules}

    # Split a large Task along a genuine operation topological order. In B/L2
    # this can expose a different core assignment without adding a Task.
    large = sorted(groups, key=lambda sg: (-work[sg], -len(groups[sg]), sg))[:6]
    new_id = max(groups) + 1
    split_count = 0
    for sg in large:
        if len(groups[sg]) < 4:
            continue
        members = [node for node in order if view["mapping"][node] == sg]
        for fraction in (1, 2):
            cut = len(members) * fraction // 3
            if not 0 < cut < len(members):
                continue
            new_mapping = dict(mapping)
            for node in members[cut:]:
                new_mapping[str(node)] = new_id
            rows = copy.deepcopy(schedules)
            source_core = view["core_by_subgraph"][sg]
            rows[source_core].insert(rows[source_core].index(sg) + 1, new_id)
            yield candidate(f"split_{sg}_{fraction}", new_mapping, rows)
            emitted += 1
            split_count += 1
            if emitted >= limit:
                return

            if split_count >= 12:
                break

    # Move costly work out of loaded cores, including the one-core plans that
    # remain common in scene A. Try early and late legal insertion positions.
    movable = sorted(groups, key=lambda sg: (-loads[view["core_by_subgraph"][sg]],
                                             -work[sg], sg))[:8]
    move_count = 0
    for sg in movable:
        source_core = view["core_by_subgraph"][sg]
        targets = sorted((c for c in range(len(schedules)) if c != source_core),
                         key=lambda c: (loads[c], c))[:2]
        for target in targets:
            for position in dict.fromkeys((0, len(schedules[target]) // 2,
                                           len(schedules[target]))):
                rows = copy.deepcopy(schedules)
                rows[source_core].remove(sg)
                rows[target].insert(position, sg)
                yield candidate(f"move_{sg}_{target}_{position}", dict(mapping), rows)
                emitted += 1
                move_count += 1
                if emitted >= limit:
                    return
                if move_count >= 16:
                    break
            if move_count >= 16:
                break
        if move_count >= 16:
            break

    # Merge neighboring same-core Tasks to remove A's boundary copies and
    # activation waits. The official parser rejects a cyclic quotient graph.
    adjacent = []
    for core, row in enumerate(schedules):
        for left, right in zip(row, row[1:]):
            adjacent.append((-(work[left] + work[right]), core, left, right))
    for _, core, left, right in sorted(adjacent)[:8]:
        rows = copy.deepcopy(schedules)
        rows[core].remove(right)
        new_mapping = {node: (left if sg == right else sg)
                       for node, sg in mapping.items()}
        yield candidate(f"merge_{left}_{right}", new_mapping, rows)
        emitted += 1
        if emitted >= limit:
            return

    # Reorder independent Tasks on the same core. Validation rejects any
    # quotient DAG or execution-order cycle introduced by the swap.
    for core, row in enumerate(schedules):
        for index in range(len(row) - 1):
            rows = copy.deepcopy(schedules)
            rows[core][index], rows[core][index + 1] = rows[core][index + 1], rows[core][index]
            yield candidate(f"swap_{core}_{index}", dict(mapping), rows)
            emitted += 1
            if emitted >= limit:
                return

def a_neighbors(graph, plan, limit=48):
    """Problem-one local search: remove expensive Task boundaries first.

    Crossing bytes are only a priority estimate. Every yielded plan is still
    checked and timed by the official scene-A evaluator.
    """
    view = derive_multicore_plan(graph, plan)
    mapping = plan["node_to_subgraph"]
    schedules = plan["core_schedules"]
    _, _, preds = graph_view(graph)
    traffic = defaultdict(int)
    for dst, links in preds.items():
        target = view["mapping"][dst]
        for src, (size, _) in links.items():
            source = view["mapping"][src]
            if source != target:
                traffic[source, target] += size

    work_by_pipe = defaultdict(lambda: defaultdict(int))
    ops = {op["id"]: op for op in graph["ops"]}
    for node, sg in view["mapping"].items():
        work_by_pipe[sg][ops[node]["pipe"]] += ops[node]["cycles"]
    work = {sg: max(pipes.values(), default=0) for sg, pipes in work_by_pipe.items()}
    loads = [sum(work[sg] for sg in row) for row in schedules]
    emitted = 0

    adjacent = []
    for core, row in enumerate(schedules):
        for left, right in zip(row, row[1:]):
            adjacent.append((-traffic[left, right] - traffic[right, left],
                             -(work[left] + work[right]), core, left, right))
    for _, _, core, left, right in sorted(adjacent)[:8]:
        rows = copy.deepcopy(schedules)
        rows[core].remove(right)
        new_mapping = {node: (left if sg == right else sg)
                       for node, sg in mapping.items()}
        yield f"merge_{left}_{right}", {"node_to_subgraph": new_mapping,
                                        "core_schedules": rows}
        emitted += 1
        if emitted >= limit:
            return

    # Move the largest work items from the busiest cores to the least loaded.
    movable = sorted(work, key=lambda sg: (-loads[view["core_by_subgraph"][sg]],
                                           -work[sg], sg))[:8]
    for sg in movable:
        source = view["core_by_subgraph"][sg]
        targets = sorted((core for core in range(len(schedules)) if core != source),
                         key=lambda core: (loads[core], core))[:2]
        for target in targets:
            positions = dict.fromkeys((0, len(schedules[target]) // 2,
                                       len(schedules[target])))
            for position in positions:
                rows = copy.deepcopy(schedules)
                rows[source].remove(sg)
                rows[target].insert(position, sg)
                yield f"move_{sg}_{target}_{position}", {
                    "node_to_subgraph": dict(mapping), "core_schedules": rows}
                emitted += 1
                if emitted >= limit:
                    return

    # Retain split and reorder moves as escape routes after coarsening.
    for label, candidate in neighbors(graph, plan, "A", limit=48):
        if label.startswith(("split_", "swap_")):
            yield label, candidate
            emitted += 1
            if emitted >= limit:
                return
