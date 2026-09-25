"""Incremental search with a portable official-score archive and hard deadlines."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from datetime import datetime

from official import ROOT, DATA, read_json, atomic_json, canonical_hash, score, settings, file_hash
from archive import FOLDERS, paths as job_paths, load_pack, save_pack, verify_pack, identity, clean
from legacy_run import summarize, validate, complete_job
from branching import candidates
from refine import partition_variants
from tensor_model import memory_order


def kill_tree(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        import signal
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def evaluate_timed(run, case, cores, scene, plan, seconds):
    directory = run/'scratch'/f'{case}_{cores}'
    directory.mkdir(parents=True, exist_ok=True)
    req, out = directory/'request.json', directory/'result.json'
    atomic_json(req, {'case':case, 'scene':scene, 'plan':plan})
    out.unlink(missing_ok=True)
    with (directory/'evaluator.log').open('w',encoding='utf-8') as log:
        p = subprocess.Popen([sys.executable,str(ROOT/'eval_one.py'),str(req),str(out)],
                             stdout=log, stderr=log, start_new_session=os.name != 'nt')
        try:
            p.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            kill_tree(p)
            return {'ok':False, 'error':f'evaluation timeout ({seconds:.1f}s)'}
        except BaseException:
            kill_tree(p)
            raise
    return read_json(out) if out.exists() else {'ok':False,'error':f'evaluator exit {p.returncode}'}


def worker(run, case, cores):
    manifest = read_json(run/'manifest.json')
    opts = manifest['options']
    archive_path = run/'cache'/f'{case}_{cores}.json.gz'
    pack = load_pack(archive_path if archive_path.exists() else ROOT/'seedpack'/archive_path.name)
    single = read_json(run/'baseline/jobs'/f'{case}.json')['makespan']
    graph = None
    config = settings()
    large = manifest['sizes'][case] > 5000
    requested_budget = opts['large_budget'] if large else opts['small_budget']
    wall = opts['large_seconds'] if large else opts['small_seconds']
    eval_limit = opts['large_eval_seconds'] if large else opts['eval_seconds']
    scenes = [s for s in FOLDERS if case in manifest['scene_cases'][s]]
    for scene in scenes:
        jp, pp = job_paths(run,case,cores,scene)
        prior = read_json(jp) if jp.exists() else None
        if prior and prior.get('search_complete') and complete_job(run,case,cores,scene):
            continue
        started = time.monotonic()
        budget_offset = prior.get('budget_offset',0) if prior else 0
        budget = requested_budget + budget_offset
        data = pack[scene]
        best = data['best']
        calls = prior.get('official_evaluations',0) if prior else 0
        pair_attempts = prior.get('pair_attempts',0) if prior else 0
        valid = prior.get('evaluation_counts',{}).get('official_valid',0) if prior else 0
        errors = prior.get('errors',[]) if prior else []
        hits = prior.get('cache_hits',0) if prior else 0
        pair, b_hash = None,None
        seen = set()

        def checkpoint(done=False, reason='searching'):
            nonlocal best
            data['best'] = best
            pool = sorted(data['leaders'],key=lambda x:score(x['metrics']))[:4]
            # Save archive before publishing a new winner, allowing safe recovery.
            save_pack(archive_path,pack)
            evaluations = [{'label':x['label'], 'plan_hash':x['plan_hash'],
                            'provenance':x.get('origin','current'), **x['metrics']} for x in pool]
            if not any(e['label'] == best['label'] and score(e)==score(best['metrics']) for e in evaluations):
                evaluations.append({'label':best['label'],'plan_hash':best['plan_hash'],**best['metrics']})
            row = {'case':case,'cores':cores,'scene':scene,'status':'ok',
                   'selected_source':best['label'],'selected':best['metrics'],
                   'selected_plan_hash':best['plan_hash'], 'paired':pair,'b_plan_hash':b_hash,
                   'leader_pool':[{**x,'score':list(score(x['metrics']))} for x in pool],
                   'evaluations':evaluations,'errors':errors,
                   'official_evaluations':calls,'budget':budget + pair_attempts,
                   'pair_attempts':pair_attempts,
                   'budget_offset':budget_offset,
                   'evaluation_counts':{'official_evaluations':calls,'official_valid':valid,
                                        'official_invalid':calls-valid},
                   'cache_hits':hits, 'search_complete':done, 'stop_reason':reason,
                   'total_seconds':round(time.monotonic()-started,3),
                   'provenance':best.get('origin','current')}
            atomic_json(pp,best['plan'])
            atomic_json(jp,row)

        def consider(label, plan, required=False):
            nonlocal calls,valid,best,hits,pair_attempts
            digest = canonical_hash(plan)
            if digest in seen and not required:
                return None
            seen.add(digest)
            record = data['cache'].get(digest)
            if record:
                hits += 1
                metric = record['metrics']
                origin = record['origin']
            else:
                remaining = wall - (time.monotonic()-started)
                if remaining <= 0 or (not required and calls >= budget + pair_attempts):
                    return None
                if required:
                    pair_attempts += 1
                calls += 1
                result = evaluate_timed(run,case,cores,scene,plan,min(eval_limit,remaining))
                if not result['ok']:
                    errors.append({'label':label,'plan_hash':digest,'error':result['error']})
                    checkpoint()
                    return None
                valid += 1
                metric, origin = clean(result['metrics']), 'NPU_9 official evaluation'
                data['cache'][digest] = {'metrics':metric,'origin':origin}
            candidate = {'label':label,'plan':plan,'plan_hash':digest,'metrics':metric,'origin':origin}
            leaders = {x['plan_hash']:x for x in data['leaders']}
            leaders[digest] = candidate
            data['leaders'] = sorted(leaders.values(),key=lambda x:score(x['metrics']))[:4]
            if score(metric) < score(best['metrics']):
                best = candidate
            checkpoint()
            return metric

        checkpoint()
        if scene == 'L2':
            b = pack['B']['best']
            b_hash = b['plan_hash']
            yes = consider('paired_b_plan',b['plan'],required=True)
            if yes is not None:
                pair = {'no_l2':b['metrics'],'with_l2':yes,
                        'cache_speedup':b['metrics']['makespan']/yes['makespan'],
                        'evaluation_label':'paired_b_plan'}
            checkpoint()
        # Zero budget assembles historical results; pairing has one reserved call.
        strong = single / best['metrics']['makespan'] >= cores * .95
        if budget and (opts.get('polish_strong') or not strong) and calls < budget + pair_attempts and time.monotonic()-started < wall:
            if graph is None:
                graph = read_json(DATA/(case+'.json'))
            def proposals():
                if scene != 'A':
                    yield 'current_a',pack['A']['best']['plan']
                    # Polish each strong seed once before generating new regions.
                    for leader in data['leaders'][:2]:
                        yield 'seed_memory_order',memory_order(graph,leader['plan'],config)
                yield from candidates(graph,cores,scene,config)
                for label,p in partition_variants(graph,best['plan'],scene,config):
                    yield 'best_'+label,p
            try:
                iterator = iter(proposals())
                while calls < budget + pair_attempts and time.monotonic()-started < wall:
                    try:
                        label,plan = next(iterator)
                    except StopIteration:
                        break
                    # A mandatory pipe's work on one core is a safe lower bound.
                    # Prune hopeless plans without invoking the costly evaluator.
                    owner = {sg:c for c,row in enumerate(plan['core_schedules']) for sg in row}
                    loads = {}
                    mapping = plan['node_to_subgraph']
                    for op in graph['ops']:
                        if str(op['id']) in mapping:
                            key = owner[mapping[str(op['id'])]],op['pipe']
                            loads[key] = loads.get(key,0) + op['cycles']
                    if max(loads.values(),default=0) > best['metrics']['makespan']:
                        continue
                    consider(label,plan)
            except Exception as exc:
                errors.append({'error':'candidate generation: '+repr(exc)})
        done = scene != 'L2' or pair is not None
        checkpoint(done, 'pair_pending' if not done else
                   'strong_incumbent' if strong and not opts.get('polish_strong') else
                   'evaluation_budget' if calls >= budget + pair_attempts else
                   'wall_budget' if time.monotonic()-started >= wall else 'portfolio_exhausted')
        print(case,cores,scene,'best',best['metrics']['makespan'],'new_calls',calls,'cache_hits',hits,flush=True)


def parse_cases(spec):
    numbers = set()
    for part in spec.split(','):
        span = part.split('-')
        numbers.update(range(int(span[0]),int(span[-1])+1))
    if not numbers or min(numbers)<1 or max(numbers)>100:
        raise ValueError('cases must be in 1..100')
    return [f'case_{n:03d}' for n in sorted(numbers)]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path)
    p.add_argument('--cases',default='1-100')
    p.add_argument('--cores',type=int,nargs='+',default=[5,2,3,4],choices=[2,3,4,5])
    p.add_argument('--scenes',nargs='+',default=['A','B','L2'],choices=list(FOLDERS))
    p.add_argument('--size',choices=['all','small','large'],default='all')
    p.add_argument('--machine',choices=['1','2'])
    p.add_argument('--workers',type=int,default=12)
    p.add_argument('--large-workers',type=int,default=2)
    p.add_argument('--heavy-threshold',type=int,default=20000)
    p.add_argument('--small-budget',type=int,default=12)
    p.add_argument('--large-budget',type=int,default=6)
    p.add_argument('--small-seconds',type=float,default=60)
    p.add_argument('--large-seconds',type=float,default=150)
    p.add_argument('--eval-seconds',type=float,default=15)
    p.add_argument('--large-eval-seconds',type=float,default=60)
    p.add_argument('--assemble-only',action='store_true')
    p.add_argument('--polish-strong',action='store_true',help='Also search incumbents already >= 95%% of core count')
    p.add_argument('--report-only',action='store_true')
    p.add_argument('--plots',action='store_true')
    p.add_argument('--refine',action='store_true',help='On an existing run, add a new budget to selected completed jobs')
    p.add_argument('--_job',nargs=2,help=argparse.SUPPRESS)
    args = p.parse_args()
    if args._job:
        worker(args.run.resolve(),args._job[0],int(args._job[1]))
        return
    if args.refine and (not args.run or not (args.run/'manifest.json').exists()):
        p.error('--refine requires an existing --run')
    if args.report_only and (not args.run or not (args.run/'manifest.json').exists()):
        p.error('--report-only requires an existing --run')
    if min(args.workers,args.large_workers,args.small_seconds,args.large_seconds,args.eval_seconds,args.large_eval_seconds)<=0 or min(args.small_budget,args.large_budget)<0:
        p.error('workers and deadlines must be positive; budgets nonnegative')
    verify_pack()
    if args.assemble_only:
        args.small_budget = args.large_budget = 0
    if args.run and (args.run/'manifest.json').exists():
        run = args.run.resolve()
        manifest = read_json(run/'manifest.json')
        if manifest['identity'] != identity():
            raise ValueError('Resume input/evaluator identity mismatch')
        if manifest['source_hashes'] != {x.name:file_hash(x) for x in ROOT.glob('*.py')}:
            raise ValueError('Resume algorithm changed; create a new run instead')
        if manifest['seed_manifest_hash'] != file_hash(ROOT/'seedpack/manifest.json'):
            raise ValueError('Resume seed archive changed')
        if args.refine:
            selected_cases = set(parse_cases(args.cases)) & set(manifest['cases'])
            selected_cases = {c for c in selected_cases if args.size=='all' or
                              (manifest['sizes'][c]>5000)==(args.size=='large')}
            for s in args.scenes:
                for c in selected_cases & set(manifest['scene_cases'][s]):
                    for k in set(args.cores)&set(manifest['cores']):
                        jp,_ = job_paths(run,c,k,s)
                        if jp.exists():
                            job = read_json(jp)
                            job['budget_offset'] = job['official_evaluations'] - job.get('pair_attempts',0)
                            job['search_complete'] = False
                            atomic_json(jp,job)
            for key in manifest['options']:
                manifest['options'][key] = getattr(args,key)
            manifest.setdefault('refinement_passes',[]).append(
                {'cases':sorted(selected_cases),'cores':args.cores,'scenes':args.scenes,
                 'options':dict(manifest['options'])})
            atomic_json(run/'manifest.json',manifest)
    else:
        run = (args.run or ROOT/'runs'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')).resolve()
        sizes = {}
        for case in parse_cases(args.cases):
            g = read_json(DATA/(case+'.json'))
            size = sum(o['op'] not in ('COPY_IN','COPY_OUT') for o in g['ops'])
            if args.size == 'small' and size>5000 or args.size == 'large' and size<=5000:
                continue
            sizes[case] = size
        cases = list(sizes)
        if not cases:
            p.error('No cases match the selected range and size')
        scene_cases = {s:cases if s in args.scenes else [] for s in FOLDERS}
        if args.machine:
            half = [c for c in cases if (int(c[-3:])>50)==(args.machine=='1')]
            scene_cases = {'A':cases if args.machine=='1' else [],
                           'B':half if args.machine=='1' else cases,'L2':half}
        # L2 always carries its corresponding B result for a valid paired report.
        scene_cases['B'] = sorted(set(scene_cases['B'])|set(scene_cases['L2']))
        manifest = {'algorithm':'NPU_9','cases':cases,'cores':list(dict.fromkeys(args.cores)),
                    'scene_cases':scene_cases,'sizes':sizes,'identity':identity(),
                    'source_hashes':{x.name:file_hash(x) for x in ROOT.glob('*.py')},
                    'seed_manifest_hash':file_hash(ROOT/'seedpack/manifest.json'),
                    'options':{k:getattr(args,k) for k in ['small_budget','large_budget','small_seconds',
                         'large_seconds','eval_seconds','large_eval_seconds','polish_strong']},'status':'running'}
        atomic_json(run/'manifest.json',manifest)
        for case in cases:
            target = run/'baseline/jobs'/f'{case}.json'
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(ROOT/'seedpack/baseline/jobs'/f'{case}.json',target)
    manifest.setdefault('execution_sessions',[]).append(
        {'started':datetime.now().isoformat(),'workers':args.workers,'large_workers':args.large_workers,
         'heavy_threshold':args.heavy_threshold,'python':sys.version,'refine':args.refine,
         'report_only':args.report_only})
    atomic_json(run/'manifest.json',manifest)
    print('RUN='+str(run),flush=True)
    pending = [(c,k) for k in manifest['cores'] for c in manifest['cases']]
    if args.refine:
        pending = [(c,k) for c,k in pending if c in selected_cases and k in args.cores]
    active, failures = {},[]
    total_tasks, completed, last_progress = len(pending), 0, time.monotonic()
    interrupted = False
    try:
        while (pending or active) and not args.report_only:
            heavy = sum(manifest['sizes'][v[0]]>args.heavy_threshold for v in active.values())
            for task in list(pending):
                if len(active)>=args.workers:
                    break
                case,cores = task
                large = manifest['sizes'][case]>args.heavy_threshold
                if large and heavy>=args.large_workers:
                    continue
                pending.remove(task)
                logpath = run/'logs'/f'{case}_{cores}.log'
                logpath.parent.mkdir(parents=True,exist_ok=True)
                log = logpath.open('a',encoding='utf-8')
                proc = subprocess.Popen([sys.executable,'-u',str(ROOT/'run.py'),'--run',str(run),
                                         '--_job',case,str(cores)],stdout=log,stderr=log,
                                        start_new_session=os.name != 'nt')
                active[proc] = (case,cores,time.monotonic(),log)
                heavy += large
            for proc,(case,cores,start,log) in list(active.items()):
                wall = manifest['options']['large_seconds' if manifest['sizes'][case]>5000 else 'small_seconds']
                timedout = time.monotonic()-start > 3*wall+90
                if proc.poll() is None and not timedout:
                    continue
                if timedout:
                    kill_tree(proc)
                log.close()
                del active[proc]
                completed += 1
                if timedout or proc.returncode:
                    failures.append({'case':case,'cores':cores,'timeout':timedout,'exit':proc.returncode})
                print(f'job {completed}/{total_tasks}',case,cores,'timeout' if timedout else proc.returncode,flush=True)
            if active and time.monotonic()-last_progress >= 30:
                oldest = max(time.monotonic()-v[2] for v in active.values())
                print(f'progress {completed}/{total_tasks}; active={len(active)}; oldest={oldest:.0f}s',flush=True)
                last_progress = time.monotonic()
            time.sleep(.1)
    except KeyboardInterrupt:
        interrupted = True
        for proc,(_,_,_,log) in active.items():
            kill_tree(proc)
            log.close()
    failures2,_ = summarize(run,manifest['cases'],manifest['cores'])
    audit = validate(run)
    atomic_json(run/'reports/validation.json',audit)
    atomic_json(run/'reports/process_failures.json',failures)
    manifest['status'] = 'interrupted' if interrupted else 'complete' if not failures2 and audit['status']=='ok' else 'partial'
    manifest['search_completed'] = not interrupted and not failures and all(
        read_json(job_paths(run,c,k,s)[0]).get('search_complete',False)
        for s in FOLDERS for c in manifest['scene_cases'][s] for k in manifest['cores']
        if job_paths(run,c,k,s)[0].exists())
    atomic_json(run/'manifest.json',manifest)
    if args.plots:
        from reports import build_plots
        build_plots(run)
    print('STATUS='+manifest['status']+'; reports='+str(run/'reports'),flush=True)
    if interrupted:
        print(f'Interrupted; resume with --run "{run}"',flush=True)
    return 1 if failures2 or audit['status']!='ok' else 0

if __name__ == '__main__':
    raise SystemExit(main())
