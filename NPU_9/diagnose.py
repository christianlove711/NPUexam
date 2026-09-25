"""Read existing logs only; no simulation or search is launched."""
from collections import Counter
from official import ROOT, read_json, atomic_json
from archive import FOLDERS


def main():
    source = ROOT.parent/'NPU_8/runs/20260924_230957_cd6168'
    baseline = {p.stem:read_json(p)['makespan'] for p in (source/'baseline/jobs').glob('*.json')}
    result = {'source':str(source),'scenes':{}}
    for scene,folder in FOLDERS.items():
        jobs = [read_json(p) for p in (source/folder/'jobs').glob('*.json')]
        jobs = [j for j in jobs if j.get('status')=='ok']
        evaluation_time = sum(e.get('evaluation_seconds',0) for j in jobs for e in j['evaluations'])
        elapsed = sum(j['total_seconds'] for j in jobs)
        five = [j for j in jobs if j['cores']==5]
        errors = Counter(e.get('error','').split(':')[0] for j in jobs for e in j.get('errors',[]))
        result['scenes'][scene] = {
            'completed_jobs':len(jobs),'missing_jobs':400-len(jobs),
            'five_core_count':len(five),
            'five_core_partial_mean':sum(baseline[j['case']]/j['selected']['makespan'] for j in five)/len(five),
            'summed_process_hours':elapsed/3600,'summed_evaluation_hours':evaluation_time/3600,
            'evaluation_fraction':evaluation_time/elapsed,
            'largest_jobs':[{'case':j['case'],'cores':j['cores'],'seconds':j['total_seconds'],
                             'official_calls':j['official_evaluations']} for j in
                            sorted(jobs,key=lambda j:-j['total_seconds'])[:12]],
            'weak_five_core':[{'case':j['case'],'speedup':baseline[j['case']]/j['selected']['makespan'],
                              'makespan':j['selected']['makespan'],
                              'partition_bytes':j['selected'].get('partition_added_copy_bytes'),
                              'spill_bytes':j['selected'].get('spill_added_copy_bytes')} for j in
                             sorted(five,key=lambda j:baseline[j['case']]/j['selected']['makespan'])[:24]],
            'errors':dict(errors)}
    comparisons = []
    for name in ('validation_focus','validation_fusion','validation_large'):
        directory = ROOT/'analysis'/name
        for scene,folder in FOLDERS.items():
            for p in (directory/folder/'jobs').glob('*.json'):
                job = read_json(p)
                previous = source/folder/'jobs'/p.name
                if not previous.exists():
                    previous = ROOT.parent/'npu_5/runs/20260923_220123_13c740'/folder/'jobs'/p.name
                if not previous.exists():
                    continue
                old = read_json(previous)
                base = baseline[job['case']]
                comparisons.append({'run':name,'case':job['case'],'scene':scene,'cores':job['cores'],
                                    'before':base/old['selected']['makespan'],
                                    'after':base/job['selected']['makespan'],
                                    'seconds':job['total_seconds'],'official_calls':job['official_evaluations'],
                                    'selected':job['selected_source']})
    result['focused_validation'] = comparisons
    result['portfolio'] = read_json(ROOT/'seedpack/manifest.json')['five_core_best_known_mean']
    atomic_json(ROOT/'analysis/diagnosis.json',result)
    print({s:{k:v for k,v in row.items() if k in ('completed_jobs','five_core_partial_mean','evaluation_fraction')}
           for s,row in result['scenes'].items()})
    print('portfolio',result['portfolio'])

if __name__ == '__main__':
    main()
