"""Merge machine shards; refuse different B baselines or mixed algorithms."""
import argparse
from pathlib import Path
import shutil
from official import read_json, atomic_json, canonical_hash, score
from run import FOLDERS, job_paths, validate, finish_run


def merge(sources, destination, no_plots=False):
    sources = [Path(p).resolve() for p in sources]
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('output directory must not already exist')
    manifests = [read_json(p / 'manifest.json') for p in sources]
    first = manifests[0]
    chosen, baselines, hashes = {}, {}, {}
    for source, manifest in zip(sources, manifests):
        if manifest['status'] != 'complete':
            raise ValueError(f'incomplete source: {source}')
        for key in ('algorithm', 'source_hashes', 'config_sha256', 'small_budget', 'large_budget', 'cores'):
            if manifest[key] != first[key]:
                raise ValueError(f'incompatible {key}: {source}')
        audit = validate(source)
        if audit['status'] != 'ok':
            raise ValueError(f'invalid source: {audit["issues"][:3]}')
        for case in manifest['cases']:
            digest = manifest['input_hashes'][case]
            if case in hashes and hashes[case] != digest:
                raise ValueError(f'input mismatch: {case}')
            hashes[case] = digest
            path = source / 'baseline/jobs' / f'{case}.json'
            row = read_json(path)
            stable = {k: v for k, v in row.items() if k != 'evaluation_seconds'}
            if case in baselines and baselines[case][1] != stable:
                raise ValueError(f'baseline mismatch: {case}')
            baselines[case] = (path, stable)
        for scene, cases in manifest['scene_cases'].items():
            for case in cases:
                for core in manifest['cores']:
                    key = (scene, case, core)
                    job_path, plan_path = job_paths(source, case, core, scene)
                    job, plan = read_json(job_path), read_json(plan_path)
                    # Full B pool identity is checked: it affects downstream L2 search.
                    signature = (canonical_hash(plan), score(job['selected']),
                                 [(e['plan_hash'], score(e['metrics'])) for e in job.get('leader_pool', [])])
                    if key in chosen and chosen[key][2] != signature:
                        raise ValueError(f'duplicate result mismatch: {key}; do not mix B/L2 pairs')
                    chosen[key] = (job_path, plan_path, signature)
    manifest = dict(first)
    manifest.update(cases=sorted(hashes), input_hashes=hashes, machine='merged', status='running',
                    merged_from=[str(p) for p in sources],
                    scene_cases={s: sorted({c for scene, c, _ in chosen if scene == s}) for s in FOLDERS})
    destination.mkdir(parents=True)
    atomic_json(destination / 'manifest.json', manifest)
    for case, (path, _) in baselines.items():
        target = destination / 'baseline/jobs' / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for (scene, case, core), (job_path, plan_path, _) in chosen.items():
        targets = job_paths(destination, case, core, scene)
        for source, target in zip((job_path, plan_path), targets):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    if not finish_run(destination, manifest, no_plots=no_plots):
        raise ValueError(f'merged validation failed: {destination}')
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', type=Path, nargs='+')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--no-plots', action='store_true')
    args = parser.parse_args()
    print(merge(args.runs, args.output, args.no_plots))
