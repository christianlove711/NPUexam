"""Convex depth regions and branch-local topological partitions.

No official evaluator rules are changed. All proposed plans need evaluation.
"""
import heapq
from collections import defaultdict

from graph import graph_view
from partition import _topological_orders, _schedule_blocks
from affinity_a import _bundle_siblings, _acyclic_groups
from official import canonical_hash


def bounded_fusion(blocks, preds, ops, cores, factor):
    """Contract cover edges only, preserving acyclicity and a work ceiling."""
    groups = {i:list(b) for i,b in enumerate(blocks)}
    if len(groups) > 2500:
        return blocks
    mapping = {n:i for i,b in groups.items() for n in b}
    edges = {i:{} for i in groups}
    for n,links in preds.items():
        for p,(size,_) in links.items():
            a,b = mapping[p],mapping[n]
            if a != b:
                edges[a][b] = edges[a].get(b,0)+size
    work = {i:sum(max(1,ops[n]['cycles']) for n in b) for i,b in groups.items()}
    ceiling = sum(work.values()) / cores * factor
    for _ in range(min(256,len(groups))):
        choices = sorted(((-size-6000,a,b) for a,links in edges.items() for b,size in links.items()
                          if work[a]+work[b] <= ceiling))
        chosen = None
        for _,a,b in choices[:128]:
            pending = [n for n in edges[a] if n != b]
            reached = set(pending)
            while pending and b not in reached:
                n = pending.pop()
                for dst in edges[n]:
                    if dst not in reached:
                        reached.add(dst)
                        pending.append(dst)
            if b not in reached:
                chosen = a,b
                break
        if chosen is None:
            break
        a,b = chosen
        groups[a].extend(groups.pop(b))
        work[a] += work.pop(b)
        edges[a].pop(b,None)
        for dst,size in edges.pop(b).items():
            edges[a][dst] = edges[a].get(dst,0)+size
        for src,links in edges.items():
            if b in links:
                links[a] = links.get(a,0)+links.pop(b)
    return list(groups.values())


def candidates(graph, cores, scene, config):
    ops, order, preds = graph_view(graph)
    _, rank, output = _topological_orders(ops, order, preds)
    depth = {}
    succ = {n: [] for n in order}
    for n in order:
        depth[n] = 1 + max((depth[p] for p in preds[n]), default=-1)
        for p in preds[n]:
            succ[p].append(n)
    seen = set()

    def package(blocks, label, wait_scale=1.0):
        mapping = {n: i for i, b in enumerate(blocks) for n in b}
        proxy_config = dict(config)
        # Only the placement heuristic is perturbed. Official evaluation always
        # receives the unchanged contest config from official.settings().
        proxy_config['a_cross_wait'] *= wait_scale
        proxy_config['b_cross_wait'] *= wait_scale
        plan, proxy = _schedule_blocks(blocks, mapping, ops, preds, rank,
                                       cores, scene, proxy_config, output)
        digest = canonical_hash(plan)
        if digest not in seen:
            seen.add(digest)
            return label, plan

    # Leave high fan-in/out separators outside the fused regions. This keeps
    # independent heads/branches separate even when they share cheap roots.
    for cutoff in (3, 4, 6):
        barriers = {n for n in order if max(len(preds[n]), len(succ[n])) >= cutoff}
        parent = {n: n for n in order}
        def find(n):
            while parent[n] != n:
                parent[n] = parent[parent[n]]
                n = parent[n]
            return n
        for n in order:
            if n in barriers:
                continue
            for p in preds[n]:
                if p not in barriers:
                    parent[find(n)] = find(p)
        blocks = _acyclic_groups(order, preds, parent)
        blocks = _bundle_siblings(blocks, preds, ops, cores, max_nodes=max(2048, len(order)))
        result = package(blocks, f'fork_separators_{cutoff}')
        if result:
            yield result
        if cutoff == 3:
            for scale in (0.0, .25):
                result = package(blocks, f'fork_spread_{scale}', scale)
                if result:
                    yield result
        if cutoff == 3:
            for factor in (0.5, 1.0, 2.0):
                merged = bounded_fusion(blocks, preds, ops, cores, factor)
                result = package(merged, f'fork_fusion_{cutoff}_{factor}')
                if result:
                    yield result

    # Components inside consecutive depth bands cannot form quotient cycles.
    # Unlike unrestricted edge contraction this never absorbs an entire fork/join
    # graph just because all its individual operators are cheap.
    for width in (16, 8, 32, 4, 64, 2):
        parent = {n: n for n in order}
        def find(n):
            while parent[n] != n:
                parent[n] = parent[parent[n]]
                n = parent[n]
            return n
        for n in order:
            for p in preds[n]:
                if depth[n] // width == depth[p] // width:
                    parent[find(n)] = find(p)
        groups = defaultdict(list)
        for n in order:
            groups[find(n)].append(n)
        blocks = _bundle_siblings(list(groups.values()), preds, ops, cores,
                                  max_nodes=max(2048, len(order)))
        result = package(blocks, f'depth_islands_{width}')
        if result:
            yield result

    # Follow ready descendants before selecting an unrelated critical branch.
    # Every resulting block is a contiguous interval in a topological order.
    degree = {n: len(preds[n]) for n in order}
    ready = [(-rank[n], n) for n in order if not degree[n]]
    heapq.heapify(ready)
    local, visited, branch_order = [], set(), []
    while ready or local:
        if local:
            n = local.pop()
        else:
            _, n = heapq.heappop(ready)
        if n in visited:
            continue
        visited.add(n)
        branch_order.append(n)
        newly = []
        for dst in succ[n]:
            degree[dst] -= 1
            if not degree[dst]:
                newly.append(dst)
                heapq.heappush(ready, (-rank[dst], dst))
        local.extend(sorted(newly, key=lambda n: (rank[n], -n)))
    for factor in (2, 4, 8, 1):
        target = max(1, sum(max(1, ops[n]['cycles']) for n in order) / (cores * factor))
        blocks, block, work = [], [], 0
        for n in branch_order:
            block.append(n)
            work += max(1, ops[n]['cycles'])
            if work >= target:
                blocks.append(block)
                block, work = [], 0
        if block:
            blocks.append(block)
        result = package(blocks, f'branch_work_{factor}')
        if result:
            yield result
