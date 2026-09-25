"""Verified transfer of existing official results between separate scene runs."""
from pathlib import Path
from official import ROOT, read_json, atomic_json, file_hash, canonical_hash, score
from archive import identity, load_pack, save_pack, FOLDERS, paths


def checked_source(source):
    source = Path(source).resolve()
    m = read_json(source/'manifest.json')
    if m.get('identity') != identity():
        raise ValueError(f'Upstream official code/config/input mismatch: {source}')
    if m.get('status') != 'complete':
        raise ValueError(f'Upstream must be complete: {source}')
    from legacy_run import validate
    audit = validate(source)
    if audit['status'] != 'ok':
        raise ValueError(f'Invalid upstream: {audit["issues"][:3]}')
    return m


def transfer(pack, source, manifest, case, cores):
    """Only import scenes actually covered and validated in the source run."""
    cache_path = source/'cache'/f'{case}_{cores}.json.gz'
    cached = load_pack(cache_path) if cache_path.exists() else {}
    for scene in FOLDERS:
        if case not in manifest['scene_cases'][scene] or cores not in manifest['cores']:
            continue
        jp,pp = paths(source,case,cores,scene)
        job,plan = read_json(jp),read_json(pp)
        digest = canonical_hash(plan)
        if digest != job['selected_plan_hash']:
            raise ValueError(f'Upstream plan changed: {pp}')
        data = pack[scene]
        for h,record in cached.get(scene,{}).get('cache',{}).items():
            old = data['cache'].get(h)
            if old and score(old['metrics']) != score(record['metrics']):
                raise ValueError('Conflicting official cached score')
            data['cache'][h] = record
        entry = {'plan':plan,'plan_hash':digest,'metrics':job['selected'],
                 'label':job['selected_source'],'origin':str(source)}
        data['cache'][digest] = {'metrics':job['selected'],'origin':str(source)}
        leaders = {x['plan_hash']:x for x in data['leaders']+job['leader_pool']+[entry]}
        data['leaders'] = sorted(leaders.values(),key=lambda x:score(x['metrics']))[:4]
        data['best'] = data['leaders'][0]
        # Frozen B must match the requested source, including equal-score ties.
        if scene == 'B':
            data['best'] = entry
        data['inherited_errors'] = job.get('errors',[])
    return pack


def promote(source):
    source = Path(source).resolve()
    m = checked_source(source)
    seed = ROOT/'seedpack'
    metadata = read_json(seed/'manifest.json')
    for case in m['cases']:
        original = read_json(seed/'baseline/jobs'/f'{case}.json')
        supplied = read_json(source/'baseline/jobs'/f'{case}.json')
        if score(original) != score(supplied):
            raise ValueError('Baseline disagreement')
        for cores in m['cores']:
            path = seed/f'{case}_{cores}.json.gz'
            save_pack(path,transfer(load_pack(path),source,m,case,cores))
            metadata['files'][path.name] = file_hash(path)
    metadata['sources'].append(str(source))
    totals = {s:[] for s in FOLDERS}
    for case in metadata['identity']['input_hashes']:
        base = read_json(seed/'baseline/jobs'/f'{case}.json')['makespan']
        pack = load_pack(seed/f'{case}_5.json.gz')
        for s in FOLDERS:
            totals[s].append(base/pack[s]['best']['metrics']['makespan'])
    metadata['five_core_best_known_mean'] = {s:sum(v)/len(v) for s,v in totals.items()}
    atomic_json(seed/'manifest.json',metadata)
    print(metadata['five_core_best_known_mean'])

if __name__ == '__main__':
    import sys
    promote(sys.argv[1])
