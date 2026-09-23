"""Analyze one complete official 100-case run of the third-version solver."""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter
from pathlib import Path


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def summarize(run):
    manifest = read_json(run / 'manifest.json')
    cases = manifest['cases']
    cores = manifest['cores']
    if len(cases) != 100 or cores != [2, 3, 4, 5]:
        raise ValueError('This report requires all 100 cases and cores 2–5')
    failures = read_csv(run / 'reports' / 'failures.csv')
    if failures:
        raise ValueError(f'{len(failures)} required jobs failed or are missing')
    baseline = read_csv(run / 'baseline' / 'singlecore.csv')
    if len(baseline) != 100:
        raise ValueError('Incomplete single-core baseline')

    lines = ['# 第三版全量模型分析', '',
             '本报告只读取当前运行目录保存的官方评估记录。每问应有 100 个用例 × 4 种多核配置。',
             '“旧候选”指同一次任务中实际评估的第一版和第二版方案；第三版保留它们并加入新候选。',
             '新增候选的贡献按相同用例、核数、场景的旧候选最优时间与最终时间之比计算。', '']
    for scene, folder in (('A', 'problem_1'), ('B', 'problem_2')):
        by_core = {core: [] for core in cores}
        for case in cases:
            for core in cores:
                job = read_json(run / folder / 'jobs' / f'{case}_{core}core.json')
                if job.get('status') != 'ok':
                    raise ValueError(f'Unsuccessful job: {scene} {case} {core}')
                old = [item for item in job['evaluations']
                       if item['label'] == 'v1' or item['label'].startswith('v2_')]
                if not old:
                    raise ValueError(f'Missing old candidates: {scene} {case} {core}')
                old_best = min(old, key=lambda row: (row['makespan'],
                                                    row['added_copy_bytes']))
                selected = job['selected']
                if selected['makespan'] > old_best['makespan']:
                    raise ValueError(f'Unexpected regression: {scene} {case} {core}')
                by_core[core].append((case, job['selected_source'],
                                      old_best['makespan'] / selected['makespan']))
        lines += [f'## 问题{"一" if scene == "A" else "二"}', '',
                  '| 核数 | 新候选入选数 / 100 | 旧候选 / 最终时间的平均比值 | 最大改善比值 |',
                  '|---:|---:|---:|---:|']
        for core, records in by_core.items():
            wins = sum(source.startswith('v3_') for _, source, _ in records)
            gains = [gain for _, _, gain in records]
            lines.append(f'| {core} | {wins} | {statistics.mean(gains):.4f} | '
                         f'{max(gains):.4f} |')
        lines += ['', '入选来源合计：' + '；'.join(
            f'{source} {count} 次' for source, count in
            Counter(source.split('_')[0] for records in by_core.values()
                    for _, source, _ in records).most_common()), '']

    summary = read_csv(run / 'reports' / 'aggregate.csv')
    lines += ['## 单核归一化与 L2 配对', '',
              '| 场景 | 核数 | 样本数 | 平均单核加速比 | 同方案 L2 平均加速比 |',
              '|---|---:|---:|---:|---:|']
    for item in summary:
        lines.append('| {scene} | {cores} | {count} | {speed} | {l2} |'.format(
            scene=item['scene'], cores=item['cores'], count=item['count'],
            speed=(f"{float(item['mean_speedup']):.4f}"
                   if item['mean_speedup'] else ''),
            l2=(f"{float(item['mean_paired_l2_speedup']):.4f}"
                if item['mean_paired_l2_speedup'] else '')))
    lines += ['', '题三的 L2 加速比使用同一份问题二方案的无 L2／有 L2 配对结果；'
              '题三另选方案的收益不得归因于 Cache 本身。', '']
    output = run / 'reports' / '第三版全量模型分析.md'
    output.write_text('\n'.join(lines), encoding='utf-8')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    print(summarize(args.run))


if __name__ == '__main__':
    main()
