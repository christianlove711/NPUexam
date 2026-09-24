"""Read both historical experiments; never run official simulation."""
from pathlib import Path
import argparse
import csv
from collections import Counter
from statistics import mean, median
from official import read_json, atomic_json, ROOT, file_hash, canonical_hash


def write_csv(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def load(run, folder, case, core):
    name = f'{case}_{core}core.json'
    job = read_json(run / folder / 'jobs' / name)
    plan = read_json(run / folder / 'plans' / name)
    assert job['status'] == 'ok' and canonical_hash(plan) == job['selected_plan_hash']
    return job, {'groups': sum(map(len, plan['core_schedules'])),
                 'active': sum(bool(row) for row in plan['core_schedules'])}


def analyze(old, new, out):
    out.mkdir(parents=True, exist_ok=True)
    old_m, new_m = read_json(old/'manifest.json'), read_json(new/'manifest.json')
    assert old_m['input_hashes'] == new_m['input_hashes']
    assert old_m['config_sha256'] == new_m['config_sha256']
    # The two runs must use identical official evaluators, regardless of OS separator.
    code = lambda m: {k.replace('\\', '/'):v for k,v in m['source_hashes'].items()
                      if k.replace('\\', '/').startswith('code/')}
    assert code(old_m) == code(new_m)
    a_rows, bc_rows, errors = [], [], Counter()
    stage_rows = []
    eval_totals = Counter()
    for case in old_m['cases']:
        base = read_json(old/'baseline/jobs'/f'{case}.json')['makespan']
        assert base == read_json(new/'baseline/jobs'/f'{case}.json')['makespan']
        for core in old_m['cores']:
            a, ap = load(old, 'problem_1', case, core)
            n, np = load(new, 'problem_1', case, core)
            b, bp = load(old, 'problem_2', case, core)
            l, lp = load(old, 'problem_3', case, core)
            x, y = a['selected'], n['selected']
            a_rows.append({'case':case, 'cores':core, 'old_makespan':x['makespan'],
                'new_makespan':y['makespan'], 'old_speedup':base/x['makespan'],
                'new_speedup':base/y['makespan'], 'speedup_delta':base/y['makespan']-base/x['makespan'],
                'old_added_bytes':x['added_copy_bytes'], 'new_added_bytes':y['added_copy_bytes'],
                'old_groups':ap['groups'], 'new_groups':np['groups'],
                'old_active':ap['active'], 'new_active':np['active'],
                'old_source':a['selected_source'], 'new_source':n['selected_source']})
            stages = {'base': [], 'refine': [], 'components': [], 'affinity': [], 'chains': []}
            for e in n['evaluations']:
                label = e['label']
                kind = ('affinity' if label.startswith('a_affinity') else 'chains' if label.startswith('a_chains')
                        else 'components' if label.startswith('components') else 'refine' if label.startswith('a_refine') else 'base')
                stages[kind].append(e['makespan'])
            best = min(stages['base'])
            row = {'case':case, 'cores':core, 'base_best':best}
            for kind in ('refine', 'components', 'affinity', 'chains'):
                after = min([best] + stages[kind])
                row[kind + '_speedup_increment'] = base/after - base/best
                best = after
            stage_rows.append(row)
            bm, lm, paired = b['selected'], l['selected'], l['paired']
            assert l['b_plan_hash'] == b['selected_plan_hash']
            assert paired['no_l2']['makespan'] == bm['makespan']
            bc_rows.append({'case':case, 'cores':core, 'a_new_makespan':y['makespan'],
                'b_makespan':bm['makespan'], 'b_speedup':base/bm['makespan'],
                'b_added_bytes':bm['added_copy_bytes'], 'b_copy_amplification':bm['scheduled_copy_bytes']/max(1,bm['original_graph_copy_bytes']),
                'b_active':bp['active'], 'b_groups':bp['groups'], 'b_source':b['selected_source'],
                'b_peak_L1':max((p['L1'] for p in bm['memory_peak_by_core'].values()),default=0),
                'b_peak_UB':max((p['UB'] for p in bm['memory_peak_by_core'].values()),default=0),
                'b_calls':b['official_evaluations'], 'b_valid':len(b['evaluations']), 'b_errors':len(b['errors']),
                'b_cycle_errors':sum('global execution' in e['error'] and 'cycle' in e['error'] for e in b['errors']),
                'l2_makespan':lm['makespan'], 'l2_speedup':base/lm['makespan'],
                'paired_makespan':paired['with_l2']['makespan'], 'cache_gain':paired['cache_speedup'],
                'paired_hit_rate':paired['with_l2']['cache_hit_rate'], 'selected_hit_rate':lm['cache_hit_rate'],
                'selected_added_bytes':lm['added_copy_bytes'], 'selected_source':l['selected_source'],
                'l2_extra_search_gain':paired['with_l2']['makespan']/lm['makespan'],
                'l2_calls':l['official_evaluations'], 'l2_errors':len(l['errors'])})
            for scene, job in [('old_A',a), ('new_A',n), ('B',b), ('L2',l)]:
                eval_totals[scene+'_calls'] += job['official_evaluations']
                eval_totals[scene+'_recorded_valid'] += len(job['evaluations'])
                for e in job['errors']:
                    msg=e['error']
                    kind=('execution_cycle' if 'global execution' in msg and 'cycle' in msg else
                          'quotient_cycle' if 'cycle' in msg.lower() or 'cyclic' in msg.lower() else
                          'order' if 'order' in msg else 'other')
                    errors[scene+'/'+kind] += 1
    summary = {'identity':'same official inputs/config/code and singlecore baselines',
               'evaluation_totals':dict(eval_totals), 'errors':dict(errors), 'cores':{}}
    for c in old_m['cores']:
        aa=[r for r in a_rows if r['cores']==c]; bb=[r for r in bc_rows if r['cores']==c]
        st=[r for r in stage_rows if r['cores']==c]
        summary['cores'][c]={'old_A':mean(r['old_speedup'] for r in aa), 'new_A':mean(r['new_speedup'] for r in aa),
            'A_improved':sum(r['new_makespan']<r['old_makespan'] for r in aa),
            'A_regressed':sum(r['new_makespan']>r['old_makespan'] for r in aa),
            'A_equal':sum(r['new_makespan']==r['old_makespan'] for r in aa),
            'A_groups_median_before':median(r['old_groups'] for r in aa), 'A_groups_median_after':median(r['new_groups'] for r in aa),
            'A_added_bytes_reduced':sum(r['new_added_bytes']<r['old_added_bytes'] for r in aa),
            'A_stage_increments':{k:mean(r[k+'_speedup_increment'] for r in st) for k in ('refine','components','affinity','chains')},
            'B':mean(r['b_speedup'] for r in bb), 'L2':mean(r['l2_speedup'] for r in bb),
            'new_A_faster_than_old_B':sum(r['a_new_makespan']<r['b_makespan'] for r in bb),
            'B_below_1_5':sum(r['b_speedup']<1.5 for r in bb),
            'B_not_all_cores':sum(r['b_active']<c for r in bb),
            'B_amplification_median':median(r['b_copy_amplification'] for r in bb),
            'B_over_90pct_L1':sum(r['b_peak_L1']>.9*524288 for r in bb),
            'B_over_90pct_UB':sum(r['b_peak_UB']>.9*131072 for r in bb),
            'cache_gain':mean(r['cache_gain'] for r in bb),
            'cache_slower':sum(r['cache_gain']<1 for r in bb),
            'cache_same':sum(r['cache_gain']==1 for r in bb),
            'cache_faster':sum(r['cache_gain']>1 for r in bb),
            'paired_zero_hit':sum(r['paired_hit_rate']==0 for r in bb),
            'l2_search_improved':sum(r['l2_makespan']<r['paired_makespan'] for r in bb)}
    write_csv(out/'a_comparison_400.csv',a_rows)
    write_csv(out/'a_stage_selection_400.csv',stage_rows)
    write_csv(out/'bc_diagnosis_400.csv',bc_rows)
    atomic_json(out/'summary.json',summary)
    print(__import__('json').dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old',type=Path,default=ROOT.parent/'npu_5/runs/20260923_220123_13c740')
    parser.add_argument('--new',type=Path,default=ROOT.parent/'npu_5/runs/20260924_161743_fb450c')
    parser.add_argument('--output',type=Path,default=ROOT/'history_analysis')
    args=parser.parse_args()
    analyze(args.old,args.new,args.output)
