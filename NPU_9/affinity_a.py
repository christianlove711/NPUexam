"""Scene-A partitions that retain large tensors inside Tasks."""
from collections import defaultdict

from graph import graph_view
from official import canonical_hash
from partition import _schedule_blocks, _topological_orders


def _acyclic_groups(order, preds, parent):
    """Contract quotient SCCs; a connected heavy-edge cluster can have holes."""
    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    edges, reverse = {}, {}
    for node in order:
        root = find(node)
        edges.setdefault(root, set())
        reverse.setdefault(root, set())
    for dst, links in preds.items():
        for src in links:
            left, right = find(src), find(dst)
            if left != right:
                edges[left].add(right)
                reverse[right].add(left)
    visited, finish = set(), []
    for root in edges:
        if root in visited:
            continue
        visited.add(root)
        stack = [(root, iter(sorted(edges[root])))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                finish.append(node)
                stack.pop()
            elif child not in visited:
                visited.add(child)
                stack.append((child, iter(sorted(edges[child]))))
    assigned = {}
    for root in reversed(finish):
        if root in assigned:
            continue
        assigned[root] = root
        pending = [root]
        while pending:
            node = pending.pop()
            for previous in reverse[node]:
                if previous not in assigned:
                    assigned[previous] = root
                    pending.append(previous)
    groups = defaultdict(list)
    for node in order:
        groups[assigned[find(node)]].append(node)
    return list(groups.values())


def _bundle_siblings(blocks, preds, ops, bins, max_nodes=2048):
    mapping = {node: i for i, block in enumerate(blocks) for node in block}
    incoming = [set() for _ in blocks]
    outgoing = [set() for _ in blocks]
    for dst, links in preds.items():
        for src in links:
            left, right = mapping[src], mapping[dst]
            if left != right:
                outgoing[left].add(right)
                incoming[right].add(left)
    siblings = defaultdict(list)
    for index in range(len(blocks)):
        siblings[tuple(sorted(incoming[index])), tuple(sorted(outgoing[index]))].append(index)
    combined = []
    for indices in siblings.values():
        if len(indices) <= bins:
            combined.extend(blocks[i] for i in indices)
            continue
        packages = [[] for _ in range(bins)]
        loads = [0] * bins
        work = {i: sum(ops[n]["cycles"] for n in blocks[i]) for i in indices}
        for i in sorted(indices, key=lambda i: (-work[i], i)):
            target = min(range(bins), key=lambda c: (loads[c], c))
            packages[target].append(i)
            loads[target] += work[i]
        for package in packages:
            chunk = []
            for index in package:
                if chunk and len(chunk) + len(blocks[index]) > max_nodes:
                    combined.append(chunk)
                    chunk = []
                chunk.extend(blocks[index])
            if chunk:
                combined.append(chunk)
    return combined


def affinity_candidates(graph, cores, config, scene="A"):
    ops, order, preds = graph_view(graph)
    _, rank, output_size = _topological_orders(ops, order, preds)
    seen = set()
    for threshold in (64, 4096):
        parent = {node: node for node in order}

        def find(node):
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for dst, links in preds.items():
            for src, (size, _) in links.items():
                # Keep expensive tensor edges inside a Task; also fuse cheap
                # scalar reduction/control chains to avoid 100-cycle switches.
                if (size >= threshold or
                        max(ops[src]["cycles"], ops[dst]["cycles"]) <= config["a_same_wait"]):
                    parent[find(src)] = find(dst)
        blocks = _acyclic_groups(order, preds, parent)
        if len(blocks) < 2:
            continue
        for bins in (cores, 0, max(2, cores - 1)):
            pieces = _bundle_siblings(blocks, preds, ops, bins) if bins else blocks
            block_of = {node: i for i, block in enumerate(pieces) for node in block}
            plan, _ = _schedule_blocks(pieces, block_of, ops, preds, rank,
                                       cores, scene, config, output_size)
            digest = canonical_hash(plan)
            if digest not in seen:
                seen.add(digest)
                yield f"affinity_{threshold}_bundle_{bins}", plan


def chain_candidates(graph, cores, config, scene="A"):
    """Fuse only unbranched dependency chains, preserving fork/join boundaries."""
    ops, order, preds = graph_view(graph)
    successors = {node: [] for node in order}
    for dst, links in preds.items():
        for src in links:
            successors[src].append(dst)
    _, rank, output_size = _topological_orders(ops, order, preds)
    seen = set()
    for limit in (8, 32):
        mapping, blocks = {}, []
        for node in order:
            source = next(iter(preds[node])) if len(preds[node]) == 1 else None
            if (source is not None and len(successors[source]) == 1
                    and len(blocks[mapping[source]]) < limit):
                target = mapping[source]
                blocks[target].append(node)
            else:
                target = len(blocks)
                blocks.append([node])
            mapping[node] = target
        pieces = _bundle_siblings(blocks, preds, ops, cores, max_nodes=64)
        block_of = {node: i for i, block in enumerate(pieces) for node in block}
        plan, _ = _schedule_blocks(pieces, block_of, ops, preds, rank, cores,
                                   scene, config, output_size)
        digest = canonical_hash(plan)
        if digest not in seen:
            seen.add(digest)
            yield f"chains_{limit}", plan
