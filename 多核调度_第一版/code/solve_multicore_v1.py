"""Deterministic first-pass partitioner for the three multicore contest scenes.

Only the two fields accepted by the official evaluators are written.  This
module uses the contest's validation routine, but never changes evaluator code.
"""

import argparse
import heapq
import json
import math
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

# Keep all original contest code in the sibling attachment directory.
OFFICIAL_CODE = Path(os.environ.get(
    'MULTICORE_OFFICIAL_CODE',
    Path(__file__).resolve().parents[2]
    / '通用神经网络处理器下的多核调度问题  附件' / 'code'))
if not (OFFICIAL_CODE / 'evaluation_validation.py').is_file():
    raise RuntimeError('official code not found: {}; set MULTICORE_OFFICIAL_CODE'
                       .format(OFFICIAL_CODE))
sys.path.insert(0, str(OFFICIAL_CODE))

from evaluation_validation import validate_graph
from stub_multicore_cut_and_schedule import derive_multicore_plan


COPY_TYPES = {'COPY_IN', 'COPY_OUT'}
SCENE_DELAYS = {'A': (100, 1000), 'B': (0, 500), 'L2': (0, 500)}


def _graph_view(graph):
    """Return eligible-op DAG and approximate bytes on each dependency."""
    ops = {op['id']: op for op in graph['ops']}
    tensors = {tensor['id']: tensor for tensor in graph['tensors']}
    full_preds = {op_id: {} for op_id in ops}
    full_succs = {op_id: set() for op_id in ops}
    producers = defaultdict(set)
    consumers = defaultdict(set)
    for edge in graph['edges']:
        src, dst = edge['source'], edge['target']
        if src in ops and dst in ops:
            full_preds[dst][src] = (0, False)
            full_succs[src].add(dst)
        elif src in ops:
            producers[dst].add(src)
        elif dst in ops:
            consumers[src].add(dst)
    for tid, source_ids in producers.items():
        tensor = tensors[tid]
        weight = tensor['size']
        shared = weight <= 1048576 and len(consumers[tid]) > 1
        for src in source_ids:
            for dst in consumers[tid]:
                if src == dst:
                    continue
                old = full_preds[dst].get(src, (0, False))
                full_preds[dst][src] = (max(weight, old[0]), shared or old[1])
                full_succs[src].add(dst)

    indegree = {op_id: len(pred) for op_id, pred in full_preds.items()}
    ready = [op_id for op_id, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    full_order = []
    while ready:
        op_id = heapq.heappop(ready)
        full_order.append(op_id)
        for dst in full_succs[op_id]:
            indegree[dst] -= 1
            if indegree[dst] == 0:
                heapq.heappush(ready, dst)
    if len(full_order) != len(ops):
        raise ValueError('operation graph contains a cycle')

    # A COPY chain is contracted into the nearest upstream non-COPY ops.
    upstream = {}
    eligible_preds = {}
    for op_id in full_order:
        merged = {}
        for pred, (weight, shared) in full_preds[op_id].items():
            sources = (upstream[pred] if ops[pred]['op'] in COPY_TYPES
                       else {pred: (0, False)})
            for src, (prior_weight, prior_shared) in sources.items():
                old = merged.get(src, (0, False))
                merged[src] = (max(old[0], weight, prior_weight),
                               old[1] or shared or prior_shared)
        if ops[op_id]['op'] in COPY_TYPES:
            upstream[op_id] = merged
        else:
            eligible_preds[op_id] = merged

    eligible = [op_id for op_id in full_order if op_id in eligible_preds]
    succs = {op_id: set() for op_id in eligible}
    for dst, pred_map in eligible_preds.items():
        for src in pred_map:
            succs[src].add(dst)
    # Critical-path priority helps keep long chains ahead of unrelated work.
    rank = {}
    for op_id in reversed(eligible):
        rank[op_id] = max(1, ops[op_id]['cycles']) + max(
            (rank[dst] for dst in succs[op_id]), default=0)
    degree = {op_id: len(eligible_preds[op_id]) for op_id in eligible}
    ready = [(-rank[op_id], op_id) for op_id in eligible if degree[op_id] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        _, op_id = heapq.heappop(ready)
        order.append(op_id)
        for dst in succs[op_id]:
            degree[dst] -= 1
            if degree[dst] == 0:
                heapq.heappush(ready, (-rank[dst], dst))
    if len(order) != len(eligible):
        raise ValueError('contracted operation graph contains a cycle')
    return ops, order, eligible_preds


def _partition(ops, order, preds, num_cores):
    if not order:
        return [], {}
    count = len(order)
    target_ops = max(32, min(512, math.ceil(count / (num_cores * 8))))
    position = {op_id: i for i, op_id in enumerate(order)}
    work_prefix = [0]
    for op_id in order:
        work_prefix.append(work_prefix[-1] + max(1, ops[op_id]['cycles']))
    target_work = work_prefix[-1] / max(1, math.ceil(count / target_ops))
    crossing_delta = [0] * (count + 1)
    for dst, pred_map in preds.items():
        right = position[dst]
        for src, (weight, _) in pred_map.items():
            left = position[src]
            crossing_delta[left + 1] += weight
            crossing_delta[right + 1] -= weight
    crossing = [0] * (count + 1)
    running = 0
    for i in range(count + 1):
        running += crossing_delta[i]
        crossing[i] = running

    bounds = [0]
    start = 0
    while count - start > int(target_ops * 1.35):
        low = start + max(16, int(target_ops * 0.75))
        high = min(count - 16, start + min(512, int(target_ops * 1.25)))
        if low > high:
            break
        cut = min(range(low, high + 1), key=lambda k: (
            abs((work_prefix[k] - work_prefix[start]) - target_work)
            / max(target_work, 1)
            + 0.15 * crossing[k] / max(60 * target_work, 1), k))
        bounds.append(cut)
        start = cut
    bounds.append(count)
    blocks = [order[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]
    block_of = {op_id: i for i, block in enumerate(blocks) for op_id in block}
    return blocks, block_of


def generate_plan(graph, num_cores=4, scene='A'):
    if scene not in SCENE_DELAYS:
        raise ValueError('scene must be A, B, or L2')
    if not 2 <= num_cores <= 5:
        raise ValueError('num_cores must be between 2 and 5')
    validate_graph(graph)
    ops, order, preds = _graph_view(graph)
    blocks, block_of = _partition(ops, order, preds, num_cores)
    incoming = [defaultdict(lambda: [0, False]) for _ in blocks]
    for dst, pred_map in preds.items():
        dst_block = block_of[dst]
        for src, (size, shared) in pred_map.items():
            src_block = block_of[src]
            if src_block == dst_block:
                continue
            entry = incoming[dst_block][src_block]
            entry[0] += size
            entry[1] |= shared

    schedules = [[] for _ in range(num_cores)]
    core_end = [0.0] * num_cores
    block_end = [0.0] * len(blocks)
    block_core = [-1] * len(blocks)
    same_wait, cross_wait = SCENE_DELAYS[scene]
    for block_id, block in enumerate(blocks):
        pipe_work = defaultdict(float)
        for op_id in block:
            pipe_work[ops[op_id]['pipe']] += max(1, ops[op_id]['cycles'])
        block_work = max(pipe_work.values(), default=0) + 0.15 * (
            sum(pipe_work.values()) - max(pipe_work.values(), default=0))
        choices = []
        for core in range(num_cores):
            ready_at = core_end[core]
            for prev, (size, shared) in incoming[block_id].items():
                same = block_core[prev] == core
                if scene == 'A':
                    transfer = size / 60
                    delay = same_wait if same else cross_wait
                elif same:
                    transfer, delay = 0, 0
                else:
                    transfer = size / 60
                    if scene == 'L2' and shared:
                        transfer *= 0.4
                    delay = cross_wait
                ready_at = max(ready_at, block_end[prev] + transfer + delay)
            finish = ready_at + block_work
            choices.append((finish, core_end[core], core))
        finish, _, core = min(choices)
        schedules[core].append(block_id)
        core_end[core] = finish
        block_end[block_id] = finish
        block_core[block_id] = core

    plan = {
        'node_to_subgraph': {str(op_id): block_of[op_id] for op_id in order},
        'core_schedules': schedules,
    }
    derive_multicore_plan(graph, plan)
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('graph', type=Path)
    parser.add_argument('-n', '--num-cores', type=int, default=4)
    parser.add_argument('--scene', choices=SCENE_DELAYS, default='A')
    parser.add_argument('-o', '--output', type=Path)
    args = parser.parse_args(argv)
    with args.graph.open(encoding='utf-8') as stream:
        graph = json.load(stream)
    started = time.perf_counter()
    plan = generate_plan(graph, args.num_cores, args.scene)
    output = args.output or (Path(__file__).resolve().parents[1] / 'results'
                             / '{}_{}_{}core_multicore_res.json'.format(
                                 args.graph.stem, args.scene, args.num_cores))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', encoding='utf-8') as stream:
        json.dump(plan, stream, ensure_ascii=False, separators=(',', ':'))
    print('plan={} scene={} cores={} subgraphs={} seconds={:.3f}'.format(
        output, args.scene, args.num_cores,
        sum(map(len, plan['core_schedules'])), time.perf_counter() - started))


if __name__ == '__main__':
    main()
