import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from worker import ATTACHMENT, read_json, settings, attempt, score, atomic_json
from solver_v4 import candidates

BASE = ATTACHMENT / '多核调度_第三版/runs/20260923_123805_9a3ab0'
OUT = Path(__file__).parent / 'pilot_results'

def job(case, core, scene):
    graph = read_json(ATTACHMENT / 'data' / f'{case}.json')
    folder = {'A': 'problem_1', 'B': 'problem_2'}[scene]
    old = read_json(BASE / folder / 'jobs' / f'{case}_{core}core.json')
    evaluations, errors = [], []
    best = old['selected']
    label = 'v3_seed'
    config = settings()
    for name, plan in candidates(graph, core):
        result = attempt(graph, plan, scene, config, name, evaluations, errors)
        if result and score(result) < score(best):
            best, label = result, name
            atomic_json(OUT / 'plans' / f'{case}_{core}_{scene}.json', plan)
    row = dict(case=case, cores=core, scene=scene, old=old['selected'], selected=best,
               source=label, evaluations=evaluations, errors=errors)
    atomic_json(OUT / f'{case}_{core}_{scene}.json', row)
    return case, scene, label, old['selected']['makespan'] / best['makespan']

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', nargs='+', default=['case_001','case_005','case_015','case_019','case_024','case_026','case_035','case_051'])
    parser.add_argument('--cores', type=int, default=5)
    args = parser.parse_args()
    with ProcessPoolExecutor(max_workers=4) as pool:
        tasks = [pool.submit(job, case, args.cores, scene) for case in args.cases for scene in ('A','B')]
        for future in as_completed(tasks):
            print(future.result(), flush=True)
