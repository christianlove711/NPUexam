"""Bounded development probe, not a full experiment."""
import subprocess
import sys
import time
from official import ROOT, DATA, read_json, atomic_json, settings
from branching import candidates

if __name__ == '__main__':
    results = []
    work = ROOT / 'analysis' / 'probe'
    work.mkdir(parents=True, exist_ok=True)
    for case in sys.argv[1:] or ['case_064', 'case_071', 'case_005']:
        graph = read_json(DATA / (case + '.json'))
        for scene in ('A', 'B', 'L2'):
            for label, plan in candidates(graph, 5, scene, settings()):
                req, out = work / 'request.json', work / 'result.json'
                atomic_json(req, {'case': case, 'scene': scene, 'plan': plan})
                out.unlink(missing_ok=True)
                started = time.perf_counter()
                try:
                    subprocess.run([sys.executable, str(ROOT/'eval_one.py'), str(req), str(out)],
                                   timeout=12, check=True, capture_output=True)
                    result = read_json(out)
                except subprocess.TimeoutExpired:
                    result = {'ok': False, 'error': 'timeout'}
                row = {'case': case, 'scene': scene, 'label': label,
                       'seconds': round(time.perf_counter()-started, 3), **result}
                results.append(row)
                if result['ok']:
                    atomic_json(work/f'{case}_{scene}_{label}.json', plan)
                atomic_json(work/'results.json', results)
                print(case, scene, label, result.get('metrics', {}).get('makespan', result.get('error')), flush=True)
