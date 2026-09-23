"""Generate v1 plans and collect official multicore evaluator metrics.

Example: python code/batch_v1.py data/case_019.json -n 2 3 4 5
"""

import argparse
import csv
import json
import time
import traceback
from pathlib import Path

from solve_multicore_v1 import generate_plan
from evaluation_validation import read_evaluation_config
from multicore_cut_evaluate_problem_1 import evaluate_scene_a, read_scene_a_config
from multicore_cut_evaluate_problem_2 import evaluate_scene_b, read_scene_b_config
from multicore_cut_evaluate_problem_3 import evaluate_problem_3, read_cache_config


FIELDS = ('case', 'scene', 'cores', 'status', 'subgraphs', 'generation_seconds',
          'evaluation_seconds', 'makespan', 'added_copy_bytes', 'cache_hit_rate',
          'error')


def evaluate(graph, plan, scene, settings, scene_a, scene_b, cache):
    common = {'bandwidth': settings['bandwidth'],
              'capacity': settings['capacity']}
    if scene == 'A':
        return evaluate_scene_a(
            graph, plan, **common,
            cross_core_wait=scene_a['task_cross_core_wait_cycles'],
            same_core_wait=scene_a['task_same_core_wait_cycles'])
    if scene == 'B':
        return evaluate_scene_b(
            graph, plan, **common,
            cross_core_copy_delay=scene_b['cross_core_copy_delay_cycles'])
    return evaluate_problem_3(
        graph, plan, **common,
        cross_core_copy_delay=scene_b['cross_core_copy_delay_cycles'], **cache)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('graphs', nargs='+', type=Path)
    parser.add_argument('-n', '--num-cores', nargs='+', type=int,
                        default=[2, 3, 4, 5])
    parser.add_argument('--scenes', nargs='+', choices=['A', 'B', 'L2'],
                        default=['A', 'B', 'L2'])
    parser.add_argument('--config', type=Path,
                        help='defaults to the first graph directory/config.txt')
    parser.add_argument('--output-dir', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'results')
    args = parser.parse_args(argv)
    if any(n < 2 or n > 5 for n in args.num_cores):
        parser.error('all core counts must be between 2 and 5')
    config = args.config or args.graphs[0].parent / 'config.txt'
    settings = read_evaluation_config(str(config))
    scene_a = read_scene_a_config(str(config))
    scene_b = read_scene_b_config(str(config))
    cache = read_cache_config(str(config))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for graph_path in args.graphs:
        with graph_path.open(encoding='utf-8') as stream:
            graph = json.load(stream)
        for scene in args.scenes:
            for cores in args.num_cores:
                row = {key: '' for key in FIELDS}
                row.update(case=graph_path.stem, scene=scene, cores=cores)
                prefix = '{}_{}_{}core'.format(graph_path.stem, scene, cores)
                try:
                    start = time.perf_counter()
                    plan = generate_plan(graph, cores, scene)
                    row['generation_seconds'] = round(time.perf_counter() - start, 3)
                    row['subgraphs'] = sum(map(len, plan['core_schedules']))
                    plan_path = args.output_dir / (prefix + '_multicore_res.json')
                    with plan_path.open('w', encoding='utf-8') as stream:
                        json.dump(plan, stream, ensure_ascii=False,
                                  separators=(',', ':'))
                    start = time.perf_counter()
                    result = evaluate(graph, plan, scene, settings,
                                      scene_a, scene_b, cache)
                    row['evaluation_seconds'] = round(time.perf_counter() - start, 3)
                    row['makespan'] = result['makespan']
                    row['added_copy_bytes'] = result['data_movement_bytes'][
                        'added_copy_bytes']
                    if scene == 'L2':
                        row['cache_hit_rate'] = result['cache_stats']['hit_rate']
                    row['status'] = 'ok'
                    metrics_path = args.output_dir / (prefix + '_metrics.json')
                    with metrics_path.open('w', encoding='utf-8') as stream:
                        json.dump(row, stream, ensure_ascii=False, indent=2)
                except Exception as error:
                    row['status'] = 'error'
                    row['error'] = '{}: {}'.format(type(error).__name__, error)
                    print(traceback.format_exc(), flush=True)
                rows.append(row)
                print('{case} {scene} {cores}core {status} makespan={makespan} '
                      'generation={generation_seconds}s evaluation={evaluation_seconds}s'
                      .format(**row), flush=True)
        del graph
    summary = args.output_dir / 'summary.csv'
    with summary.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print('summary={}'.format(summary))
    return 1 if any(row['status'] != 'ok' for row in rows) else 0


if __name__ == '__main__':
    raise SystemExit(main())
