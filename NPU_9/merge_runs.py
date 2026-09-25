"""Merge incremental runs while keeping the B/L2 pair from the same source."""
import argparse
from pathlib import Path
import shutil

from official import read_json, atomic_json, score
from run import FOLDERS, job_paths, validate, summarize


def merge(sources, destination):
    sources = [Path(p).resolve() for p in sources]
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Output directory must not already exist')
    manifests = [read_json(p/'manifest.json') for p in sources]
    first = manifests[0]
    entries, baselines, choices = {},{},{}
    notes = []
    for source,m in zip(sources,manifests):
        for key in ('algorithm','identity','source_hashes','seed_manifest_hash'):
            if m[key] != first[key]:
                raise ValueError(f'Incompatible {key}: {source}')
        audit = validate(source)
        if audit['status'] != 'ok':
            raise ValueError(f'Invalid source {source}: {audit["issues"][:3]}')
        for case in m['cases']:
            row = read_json(source/'baseline/jobs'/f'{case}.json')
            stable = {k:v for k,v in row.items() if k != 'evaluation_seconds'}
            if case in baselines and stable != baselines[case][1]:
                raise ValueError(f'Baseline mismatch: {case}')
            baselines[case] = source/'baseline/jobs'/f'{case}.json', stable
            for cores in m['cores']:
                for scene in FOLDERS:
                    if case not in m['scene_cases'][scene]:
                        continue
                    job = read_json(job_paths(source,case,cores,scene)[0])
                    entries.setdefault((case,cores,scene),[]).append((source,job))
    keys = {(c,k) for c,k,s in entries}
    for case,cores in sorted(keys):
        def best(scene):
            return min(entries.get((case,cores,scene),[]),key=lambda x:score(x[1]['selected']),default=None)
        a,b,l2 = best('A'),best('B'),best('L2')
        if a:
            choices[case,cores,'A'] = a[0]
        if l2:
            # Do not independently pick the fastest B from another machine:
            # its L2 pair may be entirely different after a wall-time cutoff.
            source,_ = l2
            paired_b = read_json(job_paths(source,case,cores,'B')[0])
            choices[case,cores,'B'] = choices[case,cores,'L2'] = source
            if b and score(b[1]['selected']) < score(paired_b['selected']):
                notes.append({'case':case,'cores':cores,'reason':'kept B from chosen L2 pair',
                              'unused_faster_b':str(b[0]),'paired_source':str(source)})
        elif b:
            choices[case,cores,'B'] = b[0]
    cases = sorted(baselines)
    cores = sorted({k for c,k,s in choices})
    m = dict(first)
    m.update(cases=cases,cores=cores,status='running',
             sizes={c:n for src in manifests for c,n in src['sizes'].items()},
             scene_cases={s:sorted({c for c,k,t in choices if t==s}) for s in FOLDERS},
             merged_from=[str(s) for s in sources])
    atomic_json(destination/'manifest.json',m)
    for case,(path,_) in baselines.items():
        target = destination/'baseline/jobs'/path.name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,target)
    for (case,core,scene),source in choices.items():
        for src,dst in zip(job_paths(source,case,core,scene),job_paths(destination,case,core,scene)):
            dst.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(src,dst)
    failures,_ = summarize(destination,cases,cores)
    audit = validate(destination)
    atomic_json(destination/'reports/validation.json',audit)
    atomic_json(destination/'reports/merge_choices.json',notes)
    m['status'] = 'complete' if not failures and audit['status']=='ok' else 'partial'
    m['search_completed'] = all(x.get('search_completed',False) for x in manifests)
    atomic_json(destination/'manifest.json',m)
    if m['status'] != 'complete':
        raise ValueError(f'Merge contains missing combinations: {destination}/reports')
    return destination

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('runs',nargs='+',type=Path)
    p.add_argument('--output',required=True,type=Path)
    args = p.parse_args()
    print(merge(args.runs,args.output))
