"""Additional deterministic candidates from a minimum-ID topological order.

Keep this module separate while the handoff v2 full run is in progress.  It can
be copied next to solver_v2.py after that run finishes, then added to the
officially evaluated candidate pool.
"""
from __future__ import annotations

import heapq
from collections import defaultdict

from solver_v2 import ATTACHMENT  # initializes official code import path
from stub_multicore_cut_and_schedule import (
    _build_op_adjacency, _contract_excluded_copy_nodes, derive_multicore_plan,
)


def _minimum_id_topological_order(ops, preds, succs):
    indegree = {node: len(preds[node]) for node in ops}
    ready = [node for node, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    result = []
    while ready:
        node = heapq.heappop(ready)
        result.append(node)
        for successor in succs[node]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(ready, successor)
    if len(result) != len(ops):
        raise ValueError('non-COPY operation graph is cyclic')
    return result


def _one_plan(graph, cores, block_size, ops, order, succs):
    block_of = {node: index // block_size for index, node in enumerate(order)}
    num_blocks = (len(order) + block_size - 1) // block_size
    members = [[] for _ in range(num_blocks)]
    predecessors = [set() for _ in range(num_blocks)]
    for node in order:
        source = block_of[node]
        members[source].append(node)
        for child in succs[node]:
            target = block_of[child]
            if source != target:
                predecessors[target].add(source)

    tensor_bytes = {t['id']: t['size'] for t in graph['tensors']}
    operation_ids = set(ops)
    inputs = defaultdict(set)
    for edge in graph['edges']:
        if edge['target'] in operation_ids and edge['source'] in tensor_bytes:
            inputs[edge['target']].add(edge['source'])

    # Only an ordering proxy.  It counts kernel-input volume, including on-chip
    # inputs, and must not be reported as actual DDR traffic or completion time.
    duration_proxy = []
    for block in members:
        matrix = sum(ops[node]['cycles'] for node in block
                     if ops[node]['pipe'] == 'PIPE_M')
        vector = sum(ops[node]['cycles'] for node in block
                     if ops[node]['pipe'] == 'PIPE_V')
        volume = sum(tensor_bytes[t] for node in block for t in inputs[node])
        duration_proxy.append(max(matrix, vector) + volume / 60)

    schedules = [[] for _ in range(cores)]
    available = [0.0] * cores
    finished = [0.0] * num_blocks
    assigned = [-1] * num_blocks
    for block in range(num_blocks):
        choices = []
        for core in range(cores):
            prior = available[core] + (100 if schedules[core] else 0)
            dep = max((finished[pred] +
                       (100 if assigned[pred] == core else 1000)
                       for pred in predecessors[block]), default=0.0)
            finish = max(prior, dep) + duration_proxy[block]
            choices.append((finish, available[core], core))
        finish, _, core = min(choices)
        schedules[core].append(block)
        available[core] = finished[block] = finish
        assigned[block] = core
    plan = {'node_to_subgraph': {str(node): block_of[node] for node in order},
            'core_schedules': schedules}
    derive_multicore_plan(graph, plan)
    return plan


def generate_idblock_candidates(graph, cores, scene):
    """Return official-format alternatives for later official evaluation.

The current priority is to capture fine-grained schedules on small graphs
without multiplying evaluation work on the largest 100-case inputs.
    """
    if scene not in {'A', 'B', 'L2'} or not 2 <= cores <= 5:
        raise ValueError('invalid scene or core count')
    ops = {op['id']: op for op in graph['ops']
           if op['op'] not in {'COPY_IN', 'COPY_OUT'}}
    _, full_succs = _build_op_adjacency(graph)
    preds, succs = _contract_excluded_copy_nodes(sorted(ops), full_succs)
    order = _minimum_id_topological_order(ops, preds, succs)
    count = len(ops)
    if count <= 1000:
        sizes = (32, 64)
    elif count <= 5000:
        sizes = (32, 128)
    else:
        sizes = (128, 512)
    return [(f'idblock_{size}', _one_plan(graph, cores, size, ops, order, succs))
            for size in sizes]
