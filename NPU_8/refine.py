"""Graph-driven portfolios; proxy priorities never replace official scores."""
from collections import defaultdict
from itertools import islice
import heapq

from graph import graph_view
from official import derive_multicore_plan
from partition import _schedule_blocks, _topological_orders
from solver import component_candidates, legal_insertions
from affinity_a import affinity_candidates, chain_candidates
from tensor_model import TensorModel, memory_order, locality_neighbors


def structural_candidates(graph, cores, scene, config):
    yield from component_candidates(graph, cores)
    yield from islice(affinity_candidates(graph, cores, config, scene), 4)
    yield from chain_candidates(graph, cores, config, scene)


def input_footprints(graph, mapping):
    """Trace original DDR inputs through COPY nodes to their direct users."""
    ops = {op['id']: op for op in graph['ops']}
    tensors = {t['id']: t for t in graph['tensors']}
    outgoing = defaultdict(list)
    produced = set()
    for edge in graph['edges']:
        outgoing[edge['source']].append(edge['target'])
        if edge['source'] in ops:
            produced.add(edge['target'])
    result = defaultdict(set)
    for tid, tensor in tensors.items():
        if tensor['pos'] != 'DDR' or tid in produced:
            continue
        pending, seen = [tid], set()
        while pending:
            node = pending.pop()
            if node in seen:
                continue
            seen.add(node)
            if str(node) in mapping:
                result[mapping[str(node)]].add(tid)
            elif node not in ops or ops[node]['op'] in {'COPY_IN', 'COPY_OUT'}:
                pending.extend(outgoing[node])
    return result, {tid: t['size'] for tid, t in tensors.items()}


def reorder(graph, plan, mode, config):
    """Keep placement, change ready-group order using real input identities."""
    view = derive_multicore_plan(graph, plan)
    groups = view['nodes_by_subgraph']
    owner = view['core_by_subgraph']
    ops, order, preds = graph_view(graph)
    _, rank, _ = _topological_orders(ops, order, preds)
    priority = {sg: max(rank[n] for n in nodes) for sg, nodes in groups.items()}
    inputs, sizes = TensorModel(graph).read_footprints(plan) if mode == 'cache' else ({}, {})
    degree = {sg: len(view['subgraph_preds'][sg]) for sg in groups}
    ready = [(-priority[sg], sg) for sg, d in degree.items() if not d]
    heapq.heapify(ready)
    rows = [[] for _ in plan['core_schedules']]
    recent, resident_bytes = {}, 0
    capacity = config['cache']['cache_capacity_bytes']
    while ready:
        def key(sg):
            footprint = inputs.get(sg, set())
            overlap = sum(sizes[t] for t in footprint if t in recent)
            return ((-overlap if mode == 'cache' else 0), -priority[sg], sg)
        candidates = [heapq.heappop(ready) for _ in range(min(len(ready), 64 if mode == 'cache' else 1))]
        sg = min((item[1] for item in candidates), key=key)
        for item in candidates:
            if item[1] != sg:
                heapq.heappush(ready, item)
        rows[owner[sg]].append(sg)
        for tid in sorted(inputs.get(sg, ())):
            if tid in recent or sizes[tid] > capacity:
                continue
            while recent and resident_bytes + sizes[tid] > capacity:
                oldest = next(iter(recent))
                resident_bytes -= recent.pop(oldest)
            recent[tid] = sizes[tid]
            resident_bytes += sizes[tid]
        for dst in view['subgraph_succs'][sg]:
            degree[dst] -= 1
            if degree[dst] == 0:
                heapq.heappush(ready, (-priority[dst], dst))
    return {'node_to_subgraph': dict(plan['node_to_subgraph']), 'core_schedules': rows}


def partition_variants(graph, plan, scene, config):
    """Reschedule inherited partitions for the current hardware objective."""
    ops, order, preds = graph_view(graph)
    _, rank, output = _topological_orders(ops, order, preds)
    grouped = defaultdict(list)
    for node in order:
        grouped[plan['node_to_subgraph'][str(node)]].append(node)
    blocks = [grouped[sg] for sg in sorted(grouped)]
    block_of = {n: i for i, block in enumerate(blocks) for n in block}
    candidate, _ = _schedule_blocks(blocks, block_of, ops, preds, rank,
                                    len(plan['core_schedules']), scene, config, output)
    yield 'placement', candidate
    yield 'critical_order', reorder(graph, plan, 'critical', config)
    if scene in {'B', 'L2'}:
        yield 'memory_order', memory_order(graph, plan, config)
    if scene == 'L2':
        yield 'cache_order', reorder(graph, plan, 'cache', config)


def advanced_neighbors(graph, plan, scene, config, diagnostics):
    """Bulk coarsening, weighted splits, and tensor-boundary node transfers."""
    yield from partition_variants(graph, plan, scene, config)
    view = derive_multicore_plan(graph, plan)
    mapping, schedules = plan['node_to_subgraph'], plan['core_schedules']
    ops, order, preds = graph_view(graph)
    groups = defaultdict(list)
    for node in order:
        groups[mapping[str(node)]].append(node)
    work = {sg: sum(max(1, ops[n]['cycles']) for n in ns) for sg, ns in groups.items()}
    late = {sg: i for i, sg in enumerate(diagnostics.get('late_subgraphs', []))}
    chosen = sorted(groups, key=lambda sg: (late.get(sg, 1000), -work[sg], sg))[:4]
    if scene == 'A':
        # Multiple individually neutral merges may together remove a bottleneck.
        for chunk in (2, 4):
            replacement, rows = {}, []
            for row in schedules:
                new_row = []
                for i in range(0, len(row), chunk):
                    new_row.append(row[i])
                    replacement.update({sg: row[i] for sg in row[i:i+chunk]})
                rows.append(new_row)
            yield f'batchmerge_{chunk}', {'node_to_subgraph': {n: replacement[s] for n, s in mapping.items()}, 'core_schedules': rows}
    for sg in chosen:
        members = groups[sg]
        if len(members) < 2:
            continue
        cumulative, cuts = 0, []
        for index, node in enumerate(members[:-1], 1):
            cumulative += max(1, ops[node]['cycles'])
            cuts.append((abs(cumulative - work[sg]/2), index))
        cut = min(cuts)[1]
        new_id = max(groups)+1
        new_map = dict(mapping)
        for node in members[cut:]:
            new_map[str(node)] = new_id
        rows = [list(row) for row in schedules]
        source = view['core_by_subgraph'][sg]
        rows[source].insert(rows[source].index(sg)+1, new_id)
        split = {'node_to_subgraph': new_map, 'core_schedules': rows}
        try:
            split_view = derive_multicore_plan(graph, split)
        except ValueError:
            continue
        ends = diagnostics.get('core_end', [])
        for target in sorted((c for c in range(len(rows)) if c != source),
                             key=lambda c: (ends[c] if c < len(ends) else 0, c))[:2]:
            for pos in legal_insertions(split_view, new_id, rows[target]):
                moved = [list(r) for r in rows]
                moved[source].remove(new_id)
                moved[target].insert(pos, new_id)
                yield f'worksplit_{sg}_{target}_{pos}', {'node_to_subgraph': new_map, 'core_schedules': moved}
    if scene != 'A':
        yield from locality_neighbors(graph, plan)
        # Move an interface operation, instead of an entire uneven-sized block.
        edges = []
        for dst, links in preds.items():
            right = mapping[str(dst)]
            for src, (size, _) in links.items():
                left = mapping[str(src)]
                if view['core_by_subgraph'][left] != view['core_by_subgraph'][right]:
                    edges.append((-size, src, dst, left, right))
        for _, src, dst, left, right in sorted(edges)[:8]:
            for node, old, new in ((dst, right, left), (src, left, right)):
                if len(groups[old]) <= 1:
                    continue
                new_map = dict(mapping)
                new_map[str(node)] = new
                yield f'boundary_{node}_{new}', {'node_to_subgraph': new_map, 'core_schedules': [list(r) for r in schedules]}
