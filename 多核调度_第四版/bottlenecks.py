"""Necessary compute-only lower bounds, not a model of full simulator timing."""
import argparse
import collections
import csv
from pathlib import Path
from worker import ATTACHMENT, read_json
from solver_idblocks import _minimum_id_topological_order
from stub_multicore_cut_and_schedule import _build_op_adjacency, _contract_excluded_copy_nodes

def report(run):
    manifest=read_json(run/'manifest.json')
    rows=[]
    for case in manifest['cases']:
        graph=read_json(ATTACHMENT/'data'/f'{case}.json')
        ops={o['id']:o for o in graph['ops'] if o['op'] not in {'COPY_IN','COPY_OUT'}}
        _, full=_build_op_adjacency(graph)
        preds,succs=_contract_excluded_copy_nodes(sorted(ops),full)
        order=_minimum_id_topological_order(ops,preds,succs)
        ends={}
        pipe=collections.Counter()
        for node in order:
            cycles=max(0,ops[node]['cycles'])
            ends[node]=cycles+max((ends[p] for p in preds[node]),default=0)
            pipe[ops[node]['pipe']]+=cycles
        critical=max(ends.values(),default=0)
        for core in manifest['cores']:
            bound=max(critical,max(pipe.values(),default=0)/core)
            for scene,folder in [('A','problem_1'),('B','problem_2'),('L2','problem_3')]:
                path=run/folder/'jobs'/f'{case}_{core}core.json'
                if not path.is_file():
                    continue
                actual=read_json(path)['selected']['makespan']
                if actual+1e-6<bound:
                    raise ValueError(f'Lower bound violation: {case} {core} {scene}')
                rows.append(dict(case=case,cores=core,scene=scene,ops=len(ops),
                                 critical_compute_cycles=critical,
                                 pipe_work_lower_bound=max(pipe.values(),default=0)/core,
                                 lower_bound=bound,makespan=actual,
                                 makespan_over_compute_bound=actual/bound if bound else ''))
    path=run/'reports/compute_lower_bounds.csv'
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader();writer.writerows(rows)
    print(f'{len(rows)} lower-bound comparisons; zero violations',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    report(parser.parse_args().run)
