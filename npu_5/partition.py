"""Communication-aware contiguous partitions and scene-specific core assignment."""
from __future__ import annotations
import heapq
import math
from collections import defaultdict
from official import derive_multicore_plan
from evaluation_validation import validate_graph
from graph import graph_view as _graph_view

def _topological_orders(ops, initial_order, preds):
    """返回三种拓扑序、节点下游路径优先级和输出大小代理量。

preds 的边值为 (字节数, 共享标记)；output_size 取最大出边大小，
不是节点全部输出之和，也不是实际同时驻留的内存峰值。
    """
    succs = {node: [] for node in initial_order}
    for dst, links in preds.items():
        for src in links:
            succs[src].append(dst)
    rank = {}
    for node in reversed(initial_order):
        rank[node] = max(1, ops[node]["cycles"]) + max(
            (rank[dst] for dst in succs[node]), default=0)
    shared_in = {node: sum(size for size, shared in preds[node].values() if shared)
                 for node in initial_order}
    # Output size is a cheap memory-pressure proxy. It does not claim to be a
    # physical peak; the evaluator makes the final decision.
    output_size = defaultdict(int)
    for dst, links in preds.items():
        for src, (size, _) in links.items():
            output_size[src] = max(output_size[src], size)

    orders = {}
    for mode in ("critical", "reuse", "memory"):
        indegree = {node: len(preds[node]) for node in initial_order}
        ready = []

        def priority(node):
            if mode == "critical":
                return (-rank[node], node)
            if mode == "reuse":
                return (-shared_in[node], -rank[node], node)
            return (output_size[node], -rank[node], node)

        for node in initial_order:
            if indegree[node] == 0:
                heapq.heappush(ready, (priority(node), node))
        order = []
        while ready:
            _, node = heapq.heappop(ready)
            order.append(node)
            for dst in succs[node]:
                indegree[dst] -= 1
                if indegree[dst] == 0:
                    heapq.heappush(ready, (priority(dst), dst))
        if len(order) != len(initial_order):
            raise ValueError("Contracted operation graph is cyclic")
        orders[mode] = order
    return orders, rank, output_size


def _partition(order, ops, preds, output_size, target, capacity):
    """按工作量、跨边界搬运和内存代理量切分连续拓扑区间。

返回 blocks（每块节点列表）和 block_of（节点到块的映射）。
target 是目标规模而非每块的严格大小上限；连续区间保持块间依赖无环。
    """
    count = len(order)
    if not count:
        return [], {}
    position = {node: i for i, node in enumerate(order)}
    work = [0]
    memory = [0]
    for node in order:
        work.append(work[-1] + max(1, ops[node]["cycles"]))
        memory.append(memory[-1] + output_size[node])
    delta = [0] * (count + 1)
    for dst, links in preds.items():
        right = position[dst]
        for src, (size, _) in links.items():
            left = position[src]
            delta[left + 1] += size
            delta[right + 1] -= size
    crossing = [0] * (count + 1)
    active = 0
    for i, value in enumerate(delta):
        active += value
        crossing[i] = active

    bounds = [0]
    start = 0
    average_cycles = work[-1] / count
    while count - start > int(target * 1.4):
        low = start + max(1, int(target * 0.65))
        high = min(count - 1, start + max(1, int(target * 1.3)))
        target_work = target * average_cycles
        if low > high:
            break

        def cut_cost(cut):
            block_work = work[cut] - work[start]
            block_memory = memory[cut] - memory[start]
            pressure = max(0.0, block_memory / max(capacity, 1) - 3.0)
            return (abs(block_work - target_work) / max(target_work, 1)
                    + 0.18 * crossing[cut] / max(target_work * 60, 1)
                    + 0.08 * pressure, cut)

        bounds.append(min(range(low, high + 1), key=cut_cost))
        start = bounds[-1]
    bounds.append(count)
    blocks = [order[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]
    block_of = {node: i for i, block in enumerate(blocks) for node in block}
    return blocks, block_of


def _schedule_blocks(blocks, block_of, ops, preds, rank, num_cores,
                     scene, config, output_size):
    """按就绪块优先级贪心分核，返回官方格式 plan 和近似评分。

A/B/L2 分别估计边界搬运、同核复用和共享输入折扣；这些估计不替代模拟器。
    """
    incoming = [defaultdict(lambda: [0, False]) for _ in blocks]
    successors = [set() for _ in blocks]
    for dst, links in preds.items():
        target = block_of[dst]
        for src, (size, shared) in links.items():
            source = block_of[src]
            if source == target:
                continue
            incoming[target][source][0] += size
            incoming[target][source][1] |= shared
            successors[source].add(target)
    indegree = [len(links) for links in incoming]
    block_rank = [max(rank[node] for node in block) for block in blocks]
    ready = [(-block_rank[i], i) for i, degree in enumerate(indegree) if degree == 0]
    heapq.heapify(ready)
    schedules = [[] for _ in range(num_cores)]
    core_end = [0.0] * num_cores
    end = [0.0] * len(blocks)
    assigned = [-1] * len(blocks)
    total_transfer = 0.0
    total_pressure = 0.0
    bandwidth = config["bandwidth"]
    capacity = sum(config["capacity"].values())
    while ready:
        _, block_id = heapq.heappop(ready)
        block = blocks[block_id]
        pipe_work = defaultdict(float)
        for node in block:
            pipe_work[ops[node]["pipe"]] += max(1, ops[node]["cycles"])
        dominant = max(pipe_work.values(), default=0)
        duration = dominant + 0.15 * (sum(pipe_work.values()) - dominant)
        pressure = max(0.0, sum(output_size[node] for node in block)
                       / max(capacity, 1) - 3.0)
        duration *= 1 + min(0.5, pressure * 0.03)
        total_pressure += pressure
        choices = []
        for core in range(num_cores):
            release = (core_end[core] + config["a_same_wait"]
                       if scene == "A" and schedules[core] else core_end[core])
            traffic = 0.0
            for source, (size, shared) in incoming[block_id].items():
                same = assigned[source] == core
                if scene == "A":
                    delay = (config["a_same_wait"] if same
                             else config["a_cross_wait"])
                    transfer = 2 * size / bandwidth
                elif same:
                    delay = 0
                    transfer = 0
                else:
                    delay = config["b_cross_wait"]
                    discount = 0.45 if scene == "L2" and shared else 1.0
                    transfer = 2 * size * discount / bandwidth
                release = max(release, end[source] + delay + transfer)
                traffic += transfer
            finish = release + duration
            choices.append((finish + 0.03 * traffic, finish,
                            core_end[core], core, traffic))
        _, finish, _, core, traffic = min(choices)
        schedules[core].append(block_id)
        assigned[block_id] = core
        end[block_id] = finish
        core_end[core] = finish
        total_transfer += traffic
        for target in successors[block_id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, (-block_rank[target], target))
    if any(core < 0 for core in assigned):
        raise ValueError("Block scheduling did not cover all blocks")
    score = max(core_end, default=0) + 0.15 * total_transfer + 0.01 * total_pressure
    plan = {"node_to_subgraph": {str(node): block_of[node] for node in block_of},
            "core_schedules": schedules}
    return plan, score


def generate_candidates(graph, num_cores, scene, config, limit=2):
    """Generate valid candidates in proxy-score order for official evaluation."""
    if scene not in ("A", "B", "L2"):
        raise ValueError("scene must be A, B, or L2")
    if not 2 <= num_cores <= 5:
        raise ValueError("core count must be between 2 and 5")
    validate_graph(graph)
    ops, initial, preds = _graph_view(graph)
    orders, rank, output_size = _topological_orders(ops, initial, preds)
    count = len(initial)
    adaptive = max(32, min(512, math.ceil(count / (num_cores * 7))))
    targets = sorted({max(24, min(512, int(adaptive * factor)))
                      for factor in (0.65, 1.0, 1.55, 2.25)})
    capacity = sum(config["capacity"].values())
    candidates = []
    seen = set()
    for mode, order in orders.items():
        for target in targets:
            blocks, block_of = _partition(
                order, ops, preds, output_size, target, capacity)
            plan, score = _schedule_blocks(
                blocks, block_of, ops, preds, rank, num_cores,
                scene, config, output_size)
            signature = (tuple(plan["node_to_subgraph"].items()),
                         tuple(tuple(row) for row in plan["core_schedules"]))
            if signature in seen:
                continue
            seen.add(signature)
            derive_multicore_plan(graph, plan)
            candidates.append((score, f"{mode}_{target}", plan))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [(label, plan, score) for score, label, plan in candidates[:limit]]

