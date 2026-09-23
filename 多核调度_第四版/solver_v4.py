"""Expanded deterministic granularity search; official evaluation decides winners."""
from solver_idblocks import _minimum_id_topological_order, _one_plan
from stub_multicore_cut_and_schedule import _build_op_adjacency, _contract_excluded_copy_nodes


def candidates(graph, cores, sizes=(1, 2, 4, 8, 16, 24, 48, 96, 192, 384, 768)):
    ops = {op['id']: op for op in graph['ops'] if op['op'] not in {'COPY_IN', 'COPY_OUT'}}
    _, full = _build_op_adjacency(graph)
    preds, succs = _contract_excluded_copy_nodes(sorted(ops), full)
    order = _minimum_id_topological_order(ops, preds, succs)
    seen = set()
    for size in sizes:
        size = min(size, max(1, len(order)))
        if size in seen:
            continue
        seen.add(size)
        yield f'v4_idblock_{size}', _one_plan(graph, cores, size, ops, order, succs)
