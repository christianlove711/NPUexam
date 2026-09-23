"""Candidate-specific lower bound for scene A (compute and task waits only)."""
from collections import Counter, defaultdict
from stub_multicore_cut_and_schedule import derive_multicore_plan

def bound_a(graph, plan, config):
    view = derive_multicore_plan(graph, plan)
    work = defaultdict(Counter)
    for op in graph['ops']:
        if op['op'] not in {'COPY_IN','COPY_OUT'}:
            work[view['mapping'][op['id']]][op['pipe']] += max(0,op['cycles'])
    preds = {s:dict.fromkeys(ps,0) for s,ps in view['subgraph_preds'].items()}
    for dst,links in preds.items():
        for src in links:
            if view['core_by_subgraph'][src] != view['core_by_subgraph'][dst]:
                links[src] = config['a_cross_wait']
    for schedule in plan['core_schedules']:
        for src,dst in zip(schedule,schedule[1:]):
            preds[dst][src] = max(preds[dst].get(src,0),config['a_same_wait'])
    # Plans created by the ID-block generator have globally increasing IDs.
    end = {}
    for block in sorted(work):
        if any(p not in end for p in preds[block]):
            raise ValueError('Bound requires increasing block IDs')
        end[block] = max((end[p]+lag for p,lag in preds[block].items()),default=0) + max(work[block].values(),default=0)
    return max(end.values(),default=0)
