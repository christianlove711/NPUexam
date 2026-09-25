"""Portable, content-verified evaluation archive. Scores retain provenance."""
import gzip
import json
import os
from pathlib import Path
from official import ROOT, CODE, DATA, read_json, atomic_json, file_hash, canonical_hash, score

FOLDERS = {'A': 'problem_1', 'B': 'problem_2', 'L2': 'problem_3'}
SOURCES = [ROOT.parent/'npu_5/runs/20260923_220123_13c740',
           ROOT.parent/'npu_5/runs/20260924_161743_fb450c',
           ROOT.parent/'NPU_8/runs/20260924_230957_cd6168']
VALIDATIONS = [ROOT/'analysis'/name for name in
               ('validation_focus','validation_fusion','validation_large')]

def identity():
    return {'official': {p.name: file_hash(p) for p in sorted(CODE.glob('*.py'))},
            'config_sha256': file_hash(DATA/'config.txt'),
            'input_hashes': {p.stem: file_hash(p) for p in sorted(DATA.glob('case_*.json'))}}

def paths(run, case, cores, scene):
    stem = f'{case}_{cores}core.json'
    return run/FOLDERS[scene]/'jobs'/stem, run/FOLDERS[scene]/'plans'/stem

def save_pack(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+f'.{os.getpid()}.tmp')
    with gzip.open(temp, 'wt', encoding='utf-8', compresslevel=3) as f:
        json.dump(value, f, separators=(',', ':'))
    os.replace(temp, path)

def load_pack(path):
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        return json.load(f)

def clean(metric):
    return {k:v for k,v in metric.items() if k not in ('label', 'plan_hash', 'evaluation_seconds')}

def build(destination=ROOT/'seedpack'):
    ident = identity()
    sources = []
    for source in SOURCES + [p for p in VALIDATIONS if (p/'manifest.json').exists()]:
        m = read_json(source/'manifest.json')
        if m.get('algorithm') == 'NPU_9':
            if m['identity'] != ident:
                raise ValueError(f'Validation identity mismatch: {source}')
            from legacy_run import validate
            if validate(source)['status'] != 'ok':
                raise ValueError(f'Invalid validation run: {source}')
        else:
            official = {k.replace('\\','/').split('/')[-1]:v for k,v in m['source_hashes'].items()
                        if k.replace('\\','/').startswith('code/')}
            if official != ident['official'] or m['config_sha256'] != ident['config_sha256']:
                raise ValueError(f'Official code/config mismatch: {source}')
            if any(ident['input_hashes'].get(k) != v for k,v in m['input_hashes'].items()):
                raise ValueError(f'Input mismatch: {source}')
        sources.append((source,m))
    totals = {s:[] for s in FOLDERS}
    hashes = {}
    for case in ident['input_hashes']:
        baselines = [read_json(s/'baseline/jobs'/f'{case}.json') for s,m in sources
                     if (s/'baseline/jobs'/f'{case}.json').exists()]
        if not baselines or any(b['status'] != 'ok' or
                (b['makespan'],b['added_copy_bytes']) !=
                (baselines[0]['makespan'],baselines[0]['added_copy_bytes']) for b in baselines):
            raise ValueError(f'Baseline disagreement: {case}')
        atomic_json(destination/'baseline/jobs'/f'{case}.json', baselines[-1])
        hashes[f'baseline/jobs/{case}.json'] = file_hash(destination/'baseline/jobs'/f'{case}.json')
        for cores in (2,3,4,5):
            pack = {s:{'cache':{}, 'leaders':[]} for s in FOLDERS}
            for source,m in sources:
                cache_path = source/'cache'/f'{case}_{cores}.json.gz'
                cached = load_pack(cache_path) if cache_path.exists() else {}
                for scene in FOLDERS:
                    jp, pp = paths(source,case,cores,scene)
                    if not jp.exists() or not pp.exists():
                        continue
                    job, plan = read_json(jp), read_json(pp)
                    if job.get('status') != 'ok':
                        continue
                    digest = canonical_hash(plan)
                    if job.get('selected_plan_hash') != digest:
                        raise ValueError(f'Plan checksum mismatch: {pp}')
                    if not any(e.get('label') == job['selected_source'] and
                               score(e) == score(job['selected']) for e in job['evaluations']):
                        raise ValueError(f'Missing evaluation evidence: {jp}')
                    origin = str(source.relative_to(ROOT.parent)).replace('\\','/')
                    def insert(h, metric):
                        record = {'metrics':clean(metric), 'origin':origin}
                        old = pack[scene]['cache'].get(h)
                        if old and score(old['metrics']) != score(record['metrics']):
                            raise ValueError(f'Conflicting cached score: {case} {scene} {h}')
                        pack[scene]['cache'][h] = record
                    for h,record in cached.get(scene,{}).get('cache',{}).items():
                        insert(h,record['metrics'])
                    for e in job['evaluations']:
                        if e.get('plan_hash') and 'makespan' in e:
                            insert(e['plan_hash'],e)
                    insert(digest,job['selected'])
                    candidates = [{'plan':plan, 'plan_hash':digest, 'metrics':clean(job['selected']),
                                   'label':job['selected_source'], 'origin':origin}]
                    for leader in job.get('leader_pool', []):
                        if canonical_hash(leader['plan']) != leader['plan_hash']:
                            raise ValueError(f'Leader checksum mismatch: {jp}')
                        insert(leader['plan_hash'],leader['metrics'])
                        candidates.append({**leader, 'metrics':clean(leader['metrics']), 'origin':origin})
                    byhash = {x['plan_hash']:x for x in pack[scene]['leaders']+candidates}
                    pack[scene]['leaders'] = sorted(byhash.values(), key=lambda x:score(x['metrics']))[:4]
                    if scene == 'L2' and job.get('b_plan_hash') and job.get('paired'):
                        insert(job['b_plan_hash'], job['paired']['with_l2'])
            for scene in FOLDERS:
                if not pack[scene]['leaders']:
                    raise ValueError(f'Missing incumbent: {case}/{cores}/{scene}')
                pack[scene]['best'] = pack[scene]['leaders'][0]
                if cores == 5:
                    totals[scene].append(baselines[0]['makespan']/pack[scene]['best']['metrics']['makespan'])
            name = f'{case}_{cores}.json.gz'
            save_pack(destination/name, pack)
            hashes[name] = file_hash(destination/name)
        print('archive',case,flush=True)
    manifest = {'identity':ident, 'files':hashes,
                'sources':[str(s) for s,m in sources],
                'five_core_best_known_mean':{s:sum(v)/len(v) for s,v in totals.items()},
                'note':'Historical best-known portfolio, not a new full search; L2 pairing may require evaluation.'}
    atomic_json(destination/'manifest.json',manifest)
    print(manifest['five_core_best_known_mean'])

def verify_pack(path=ROOT/'seedpack'):
    manifest = read_json(path/'manifest.json')
    if manifest['identity'] != identity():
        raise ValueError('Seed archive official code, config or graph identity changed')
    for name,digest in manifest['files'].items():
        if file_hash(path/name) != digest:
            raise ValueError(f'Seed archive checksum mismatch: {name}')
    return manifest

if __name__ == '__main__':
    build()
