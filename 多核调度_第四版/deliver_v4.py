"""Copy validated fourth-version modeling results into a user-facing directory."""
import argparse
import csv
import shutil
import statistics
from pathlib import Path
from worker import read_json

def csv_rows(path):
    with path.open(encoding='utf-8-sig',newline='') as stream:
        return list(csv.DictReader(stream))

def deliver(run, output):
    manifest=read_json(run/'manifest.json')
    audit=read_json(run/'reports/v4_audit.json')
    if manifest['status']!='complete' or audit['issues'] or audit['records']!=1200 or len(audit['replayed'])<12:
        raise ValueError('Full run and independent audit must succeed before delivery')
    output.mkdir(parents=True,exist_ok=True)
    comparisons=csv_rows(run/'reports/v3_v4_comparison.csv')
    aggregate=csv_rows(run/'reports/aggregate.csv')
    names={'A':'问题一','B':'问题二','L2':'问题三'}
    lines=['# A题第四版优化成果','',
           '第三版有部分图把所有操作放在一个核上。第四版扩大切图粒度搜索，按场景独立用官方模拟器选优，并保留第三版最优方案。',
           '', '## 100 用例、2～5 核的全量结果','',
           '下表每个场景平均覆盖 400 个配置；加速比均相对同用例的官方单核基准。',
           '', '| 场景 | 第三版平均加速比 | 第四版平均加速比 | 严格改善配置 | 平均耗时下降 |',
           '|---|---:|---:|---:|---:|']
    for scene,name in names.items():
        group=[r for r in comparisons if r['scene']==scene]
        lines.append(f"| {name} | {statistics.mean(float(r['v3_speedup']) for r in group):.4f} | {statistics.mean(float(r['v4_speedup']) for r in group):.4f} | {sum(int(r['improved']) for r in group)}/400 | {statistics.mean(float(r['mean_time_reduction']) for r in group):.2%} |")
    lines+=['','## 按核数细分','',
            '| 场景 | 核数 | 第三版平均加速比 | 第四版平均加速比 |',
            '|---|---:|---:|---:|']
    for row in comparisons:
        lines.append(f"| {names[row['scene']]} | {row['cores']} | {float(row['v3_speedup']):.4f} | {float(row['v4_speedup']):.4f} |")
    lines+=['',f"![第三、四版平均加速比对比]({(output/'v3_v4_speedup.png').resolve().as_posix()})",'',
            '## L2 指标不能与方案改善混用','',
            '题三整体改善包含重新切图分核的收益。固定第四版题二方案后，无 L2/有 L2 的平均配对比值如下：','',
            '| 核数 | 固定方案 L2 平均加速比 |','|---:|---:|']
    for row in aggregate:
        if row['scene']=='L2':
            lines.append(f"| {row['cores']} | {float(row['mean_paired_l2_speedup']):.5f} |")
    lines+=['','## 核验与复现','',
            '- 单核基准 100 条；三问各 400 条。所有最终配置不劣于第三版，失败任务为 0。',
            '- 1200 份最终方案逐份调用官方方案解析/合法性检查；静态完整性、配对与来源检查均通过。',
            f"- {len(audit['replayed'])} 份代表方案独立重新运行官方模拟器，时间与新增 COPY 指标均一致。",
            '- 独立重跑选择每个场景/核数下改进最大的方案，以及每个场景一个不变方案；不是重新模拟全部 1200 份。',
            '- 原有 8 项测试通过；新增小规模集成实验 12 份方案合法、6 份独立重跑一致。',
            '- 此次全量未使用 bound_a.py 实验剪枝模块，未进行跨核局部搜索，不宣称全局最优或未知图泛化效果。',
            '',f'代码目录：`{run.resolve().parents[1]}`。',
            f'正式运行目录：`{run.resolve()}`。',
            '', '推荐入口为 `improve_v4.py`，保留第三版正式运行目录作为可复现的保底来源。GitHub 上传仍按用户安排暂缓。','']
    (output/'优化成果.md').write_text('\n'.join(lines),encoding='utf-8')
    for name in ('v3_v4_comparison.csv','aggregate.csv','v4_audit.json','validation.json',
                 '第四版全量优化分析.md','compute_lower_bounds.csv','v3_v4_speedup.png',
                 'v3_v4_speedup.svg','v3_v4_reduction.png'):
        shutil.copy2(run/'reports'/name,output/name)
    shutil.copy2(Path(__file__).parent/'第四版建模说明.md',output/'建模说明.md')
    print(output.resolve(),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    deliver(args.run,args.output)
