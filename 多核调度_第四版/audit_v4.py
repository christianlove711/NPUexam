"""Audit saved plans, reference scores and independently replay representative winners."""
import argparse
import collections
import statistics
from pathlib import Path
from worker import read_json, atomic_json, file_hash, official_evaluate, settings, score
from improve_v4 import ATTACHMENT, BASE, FOLDERS
from stub_multicore_cut_and_schedule import derive_multicore_plan
from verify_run import validate

def audit(run, replay=False):
    manifest = read_json(run / 'manifest.json')
    issues = validate(run)
    records = []
    for case in manifest['cases']:
        graph = read_json(ATTACHMENT / 'data' / f'{case}.json')
        if file_hash(run / 'baseline/jobs' / f'{case}.json') != file_hash(BASE / 'baseline/jobs' / f'{case}.json'):
            issues.append(f'Baseline mismatch {case}')
        for core in manifest['cores']:
            for scene, folder in FOLDERS.items():
                stem = f'{case}_{core}core.json'
                job = read_json(run / folder / 'jobs' / stem)
                old = read_json(BASE / folder / 'jobs' / stem)
                plan = read_json(run / folder / 'plans' / stem)
                try:
                    derive_multicore_plan(graph, plan)
                except Exception as exc:
                    issues.append(f'Illegal plan {scene} {case} {core}: {exc}')
                if score(job['selected']) > score(old['selected']):
                    issues.append(f'Regression {scene} {case} {core}')
                if job['seed_plan_sha256'] != file_hash(BASE / folder / 'plans' / stem):
                    issues.append(f'Seed plan changed {scene} {case} {core}')
                if job['seed_job_sha256'] != file_hash(BASE / folder / 'jobs' / stem):
                    issues.append(f'Seed metrics changed {scene} {case} {core}')
                t0, t1 = old['selected']['makespan'], job['selected']['makespan']
                records.append(dict(case=case, cores=core, scene=scene,
                                    old=t0, new=t1, ratio=t0/t1,
                                    reduction=1-t1/t0, errors=job['errors'],
                                    source=job['selected_source']))
    replayed = []
    if replay:
        cfg = settings()
        # Best gain in each scene/core stratum plus one unchanged case per scene.
        chosen = set()
        for scene in FOLDERS:
            for core in manifest['cores']:
                subset = [r for r in records if r['scene']==scene and r['cores']==core]
                best = max(subset, key=lambda r:r['ratio'])
                chosen.add((scene,best['case'],core))
            equal = next((r for r in records if r['scene']==scene and r['old']==r['new']),None)
            if equal:
                chosen.add((scene,equal['case'],equal['cores']))
        for scene, case, core in sorted(chosen):
            folder = FOLDERS[scene]
            stem = f'{case}_{core}core.json'
            plan = read_json(run / folder / 'plans' / stem)
            graph = read_json(ATTACHMENT / 'data' / f'{case}.json')
            actual = official_evaluate(graph,plan,scene,cfg)
            expected = read_json(run / folder / 'jobs' / stem)['selected']
            ok = score(actual)==score(expected)
            if not ok:
                issues.append(f'Replay mismatch {scene} {case} {core}')
            replayed.append(dict(scene=scene,case=case,cores=core,ok=ok,makespan=actual['makespan']))
    atomic_json(run / 'reports/v4_audit.json', dict(issues=issues, records=len(records),replayed=replayed))
    coverage=f"{len(manifest['cases'])} 个用例，核数 {manifest['cores']}"
    lines=['# 第四版优化结果','',f'运行：`{run.name}`。对照：第三版 `20260923_123805_9a3ab0`。',
           f'覆盖范围：{coverage}。',
           '', '加速比取“同用例官方单核时间 / 当前时间”；改进比取“第三版时间 / 第四版时间”。均先逐组求比再取算术平均。',
           '平均耗时下降是逐组 `1 − 第四版时间/第三版时间` 的均值，不能用平均改进比直接换算。','',
           '| 场景 | 核数 | 样本数 | 严格改善数 | 平均改进比 | 中位改进比 | 平均耗时下降 |',
           '|---|---:|---:|---:|---:|---:|---:|']
    for scene in FOLDERS:
        for core in manifest['cores']:
            group=[r for r in records if r['scene']==scene and r['cores']==core]
            ratios=[r['ratio'] for r in group]
            lines.append(f"| {scene} | {core} | {len(group)} | {sum(r['new']<r['old'] for r in group)} | {statistics.mean(ratios):.4f} | {statistics.median(ratios):.4f} | {statistics.mean(r['reduction'] for r in group):.2%} |")
    lines+=['','## 核验与边界','',
            f'- 已调用官方方案解析/合法性检查：{len(records)} 份最终方案。',
            f'- 独立重新模拟：{len(replayed)} 份（每场景、核数改善最大者，及每场景一个不变者）。',
            f'- 一致性及非退步检查问题数：{len(issues)}。',
            f"- 候选评估错误记录数：{sum(len(r['errors']) for r in records)}；候选失败与最终任务失败需区分。",
            '- 这是确定性粒度搜索，不是全局最优证明；在公开实例上直接选优，不能声称未知图的泛化性能。',
            '- L2 配对列固定第四版题二方案。题三额外试验至多三个题二候选，整体方案收益与纯 Cache 收益应分别报告。',
            '- 第三版已完成结果作为保底直接复用，未重复计算全部旧候选；单核基准也复用相同输入/配置的官方记录。','']
    (run/'reports/第四版全量优化分析.md').write_text('\n'.join(lines),encoding='utf-8')
    print(dict(records=len(records),replayed=len(replayed),issues=issues),flush=True)
    return bool(issues)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('--replay',action='store_true')
    args=parser.parse_args()
    raise SystemExit(audit(args.run,args.replay))
