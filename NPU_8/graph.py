"""Operation DAG with COPY chains contracted and dependency bytes retained."""
from __future__ import annotations

import heapq
from collections import defaultdict

import official  # Initialize the official code import path.
from evaluation_validation import validate_graph

COPY_TYPES = {"COPY_IN", "COPY_OUT"}


def graph_view(graph):
    ops = {op["id"]: op for op in graph["ops"]}
    tensors = {tensor["id"]: tensor for tensor in graph["tensors"]}
    full_preds = {node: {} for node in ops}
    full_succs = {node: set() for node in ops}
    producers, consumers = defaultdict(set), defaultdict(set)
    for edge in graph["edges"]:
        src, dst = edge["source"], edge["target"]
        if src in ops and dst in ops:
            full_preds[dst][src] = (0, False)
            full_succs[src].add(dst)
        elif src in ops:
            producers[dst].add(src)
        elif dst in ops:
            consumers[src].add(dst)
    for tid, sources in producers.items():
        size = tensors[tid]["size"]
        shared = size <= 1048576 and len(consumers[tid]) > 1
        for src in sources:
            for dst in consumers[tid]:
                if src == dst:
                    continue
                previous = full_preds[dst].get(src, (0, False))
                full_preds[dst][src] = (max(size, previous[0]), shared or previous[1])
                full_succs[src].add(dst)
    degree = {node: len(links) for node, links in full_preds.items()}
    ready = [node for node, count in degree.items() if not count]
    heapq.heapify(ready)
    full_order = []
    while ready:
        node = heapq.heappop(ready)
        full_order.append(node)
        for dst in full_succs[node]:
            degree[dst] -= 1
            if degree[dst] == 0:
                heapq.heappush(ready, dst)
    if len(full_order) != len(ops):
        raise ValueError("operation graph is cyclic")
    upstream, eligible_preds = {}, {}
    for node in full_order:
        merged = {}
        for pred, (size, shared) in full_preds[node].items():
            sources = upstream[pred] if ops[pred]["op"] in COPY_TYPES else {pred: (0, False)}
            for src, (prior_size, prior_shared) in sources.items():
                old = merged.get(src, (0, False))
                merged[src] = (max(old[0], size, prior_size), old[1] or shared or prior_shared)
        if ops[node]["op"] in COPY_TYPES:
            upstream[node] = merged
        else:
            eligible_preds[node] = merged
    eligible = [node for node in full_order if node in eligible_preds]
    succs = {node: set() for node in eligible}
    for dst, links in eligible_preds.items():
        for src in links:
            succs[src].add(dst)
    rank = {}
    for node in reversed(eligible):
        rank[node] = max(1, ops[node]["cycles"]) + max((rank[dst] for dst in succs[node]), default=0)
    degree = {node: len(eligible_preds[node]) for node in eligible}
    ready = [(-rank[node], node) for node in eligible if degree[node] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        _, node = heapq.heappop(ready)
        order.append(node)
        for dst in succs[node]:
            degree[dst] -= 1
            if degree[dst] == 0:
                heapq.heappush(ready, (-rank[dst], dst))
    if len(order) != len(eligible):
        raise ValueError("contracted operation graph is cyclic")
    return ops, order, eligible_preds
