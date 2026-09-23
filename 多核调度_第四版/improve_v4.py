"""Reproducible incremental official evaluation, retaining the complete v3 incumbent.

AI-assisted implementation. Candidate generation is heuristic; only official
simulations determine scores. No official input or evaluator is modified.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from worker import (ATTACHMENT, atomic_json, read_json, file_hash, settings,
                    attempt, score)
from solver_v4 import candidates
from run_all import summarize
from verify_run import validate

ROOT = Path(__file__).resolve().parent
BASE = Path(os.environ.get(
    'NPU_V3_BASE_RUN',
    ATTACHMENT / '多核调度_第三版/runs/20260923_123805_9a3ab0'))
FOLDERS = {'A': 'problem_1', 'B': 'problem_2', 'L2': 'problem_3'}


def optimize(run_text, case, cores):
    run = Path(run_text)
    stem = f'{case}_{cores}core.json'
    if all((run / folder / 'jobs' / stem).is_file() for folder in FOLDERS.values()):
        return case, cores, 'cached'
    graph = read_json(ATTACHMENT / 'data' / f'{case}.json')
    config = settings()
    size = sum(op['op'] not in {'COPY_IN', 'COPY_OUT'} for op in graph['ops'])
    # Small graphs receive a dense grid; large graphs use a coarse grid followed
    # by refinement if its best candidate lies at the fine-grain boundary.
    sizes = (1, 2, 4, 8, 16, 24, 48, 96, 192, 384, 768) if size <= 5000 else (4, 16, 48, 192, 768)
    plans = dict(candidates(graph, cores, sizes))
    selected_b = None
    b_ranked = []
    changes = {}
    for scene, folder in FOLDERS.items():
        started = time.perf_counter()
        old = read_json(BASE / folder / 'jobs' / stem)
        seed = read_json(BASE / folder / 'plans' / stem)
        best = ('v3_seed', seed, old['selected'])
        evaluations = [{'label': 'v3_seed', **old['selected']}]
        errors = []
        paired, b_hash = None, None
        if scene == 'L2':
            b_path = run / 'problem_2' / 'plans' / stem
            b_hash = file_hash(b_path)
            b_plan = read_json(b_path)
            # Reuse the exact B result whose saved plan is hashed below.
            no_cache = selected_b
            cache = attempt(graph, b_plan, 'L2', config, 'paired_with_l2', evaluations, errors)
            if cache is None:
                raise RuntimeError(f'Cannot evaluate L2 pair: {case} {cores}: {errors}')
            paired = {'no_l2': no_cache, 'with_l2': cache,
                      'cache_speedup': no_cache['makespan'] / cache['makespan']}
            evaluations.append({'label': 'paired_no_l2', **no_cache})
            if score(cache) < score(best[2]):
                best = ('paired_b_plan', b_plan, cache)
            # Also try the best alternative B-granularity plans under real L2.
            trial = {name: plan for _, name, plan in sorted(b_ranked, key=lambda t: t[0])[:3]}
        else:
            trial = dict(plans)
        fingerprints = {hashlib.sha256(json.dumps(seed, sort_keys=True).encode()).hexdigest()}
        if scene == 'L2':
            fingerprints.add(hashlib.sha256(json.dumps(b_plan, sort_keys=True).encode()).hexdigest())
        tested = []

        def evaluate_trials(items):
            nonlocal best
            for label, plan in items:
                digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
                if digest in fingerprints:
                    continue
                fingerprints.add(digest)
                result = attempt(graph, plan, scene, config, label, evaluations, errors)
                if result:
                    tested.append((score(result), label, plan))
                    if score(result) < score(best[2]):
                        best = label, plan, result

        evaluate_trials(trial.items())
        if scene != 'L2' and size > 5000 and tested:
            coarse = min(tested, key=lambda t: t[0])[1]
            grain = int(coarse.rsplit('_', 1)[1])
            refine = {4: (1, 2, 8), 16: (8, 24, 32), 48: (24, 32, 96),
                      192: (96, 128, 384), 768: (384, 512, 1536)}[grain]
            evaluate_trials(candidates(graph, cores, refine))
        row = dict(case=case, cores=cores, scene=scene, status='ok',
                   selected_source=best[0], selected=best[2], v1=old['v1'], paired=paired,
                   b_plan_sha256=b_hash, generation_seconds=None,
                   total_seconds=round(time.perf_counter()-started, 3),
                   evaluations=evaluations, errors=errors,
                   v3_makespan=old['selected']['makespan'],
                   seed_job_sha256=file_hash(BASE / folder / 'jobs' / stem),
                   seed_plan_sha256=file_hash(BASE / folder / 'plans' / stem))
        atomic_json(run / folder / 'plans' / stem, best[1])
        atomic_json(run / folder / 'jobs' / stem, row)
        changes[scene] = round(old['selected']['makespan'] / best[2]['makespan'], 4)
        if scene == 'B':
            selected_b = best[2]
            b_ranked = tested
    return case, cores, changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path)
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--cores', nargs='+', type=int, default=[2,3,4,5])
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--no-plots', action='store_true')
    args = parser.parse_args()
    if not (BASE / 'manifest.json').is_file():
        parser.error(f'Missing third-version baseline {BASE}. Set NPU_V3_BASE_RUN to a complete run created by run_all.py.')
    run = args.run or ROOT / 'runs' / datetime.now().strftime('%Y%m%d_%H%M%S_v4')
    old_manifest = read_json(BASE / 'manifest.json')
    cases = args.cases or old_manifest['cases']
    manifest = dict(old_manifest)
    if (run / 'manifest.json').is_file():
        manifest = read_json(run / 'manifest.json')
        cases, args.cores = manifest['cases'], manifest['cores']
        recorded = manifest['source_hashes']
        if any(file_hash(ROOT / name) != digest for name, digest in recorded.items()):
            raise RuntimeError('Solver changed since run creation; use a new run')
    else:
        manifest.update(cases=cases, cores=args.cores, seed_run=str(BASE),
                        created_at=datetime.now().astimezone().isoformat(),
                        algorithm='v4_expanded_granularity',
                        run_parameters={'workers':args.workers, 'no_plots':args.no_plots,
                                        'large_graph_threshold':5000, 'l2_top_b_candidates':3},
                        source_hashes={name:file_hash(ROOT/name) for name in
                                       ('improve_v4.py','solver_v4.py','solver_idblocks.py','worker.py')})
        manifest.pop('finished_at', None)
        for case in cases:
            destination = run / 'baseline/jobs' / f'{case}.json'
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BASE / 'baseline/jobs' / f'{case}.json', destination)
    manifest['status'] = 'running'
    atomic_json(run / 'manifest.json', manifest)
    print(f'RUN={run}', flush=True)
    exceptions = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        tasks = [pool.submit(optimize, str(run), case, core) for case in cases for core in args.cores]
        for index, future in enumerate(as_completed(tasks), 1):
            try:
                result = future.result()
                print(f'{index}/{len(tasks)} {result}', flush=True)
            except Exception as exc:
                exceptions.append(str(exc))
                print(f'ERROR {exc}', flush=True)
    errors, _ = summarize(run, cases, args.cores)
    if not errors and not args.no_plots:
        from reports import build_plots
        build_plots(run)
    issues = validate(run, require_plots=not args.no_plots)
    atomic_json(run / 'reports/validation.json', {'status':'ok' if not issues else 'error', 'issues':issues})
    manifest.update(status='complete' if not errors and not issues and not exceptions else 'incomplete',
                    finished_at=datetime.now().astimezone().isoformat(),
                    missing_or_failed_count=len(errors), exceptions=exceptions)
    atomic_json(run / 'manifest.json', manifest)
    print(f"STATUS={manifest['status']} issues={issues} errors={errors}", flush=True)
    return 0 if manifest['status']=='complete' else 1

if __name__ == '__main__':
    raise SystemExit(main())
