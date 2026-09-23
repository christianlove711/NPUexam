"""Build paired v3/v4 scientific figures and a table from completed summaries."""
import argparse
import csv
import statistics
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from improve_v4 import BASE, FOLDERS
from worker import read_json

def rows(path):
    with path.open(encoding='utf-8-sig',newline='') as stream:
        return list(csv.DictReader(stream))

def compare(run):
    manifest=read_json(run/'manifest.json')
    if manifest['status']!='complete' or len(manifest['cases'])!=100 or manifest['cores']!=[2,3,4,5]:
        raise ValueError('Comparison requires a completed 100-case, 2–5 core run')
    table=[]
    reductions={}
    for scene,folder in FOLDERS.items():
        old={(r['case'],int(r['cores'])):r for r in rows(BASE/folder/'summary.csv')}
        new=rows(run/folder/'summary.csv')
        if len(new)!=400:
            raise ValueError(f'Incomplete {scene}')
        reductions[scene]=[]
        for core in manifest['cores']:
            group=[r for r in new if int(r['cores'])==core]
            pairs=[(old[(r['case'],core)],r) for r in group]
            delta=[1-float(n['makespan'])/float(o['makespan']) for o,n in pairs]
            reductions[scene]+=delta
            table.append(dict(scene=scene,cores=core,count=len(pairs),
                              v3_speedup=statistics.mean(float(o['speedup']) for o,n in pairs),
                              v4_speedup=statistics.mean(float(n['speedup']) for o,n in pairs),
                              improved=sum(d>0 for d in delta),
                              mean_time_reduction=statistics.mean(delta),
                              mean_time_ratio=statistics.mean(float(o['makespan'])/float(n['makespan']) for o,n in pairs)))
    with (run/'reports/v3_v4_comparison.csv').open('w',newline='',encoding='utf-8-sig') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(table[0]));writer.writeheader();writer.writerows(table)
    fig,axes=plt.subplots(1,3,figsize=(13.5,4.3),sharey=True,layout='constrained')
    for ax,scene in zip(axes,FOLDERS):
        group=[r for r in table if r['scene']==scene]
        ax.plot([r['cores'] for r in group],[r['v3_speedup'] for r in group],'o--',label='Version 3',color='#7c8797')
        ax.plot([r['cores'] for r in group],[r['v4_speedup'] for r in group],'o-',label='Version 4',color='#1769aa')
        ax.set(title=f'Scene {scene}',xlabel='Number of cores',xticks=[2,3,4,5])
        ax.grid(alpha=.25);ax.legend(frameon=False)
    axes[0].set_ylabel('Mean speedup over official single-core baseline')
    fig.suptitle('100 cases per point — arithmetic mean of per-case speedup')
    for ext in ('png','svg'):
        fig.savefig(run/f'reports/v3_v4_speedup.{ext}',dpi=180)
    plt.close(fig)
    fig,axes=plt.subplots(1,3,figsize=(13.5,4.3),sharey=True,layout='constrained')
    for ax,scene in zip(axes,FOLDERS):
        xs=sorted(100*d for d in reductions[scene])
        ax.step(xs,[(i+1)/len(xs) for i in range(len(xs))],where='post',color='#1769aa')
        ax.set(title=f'Scene {scene}: {sum(d>0 for d in reductions[scene])}/400 improved',
               xlabel='Makespan reduction relative to v3 (%)',ylim=(0,1.02),xlim=(0,max(5,max(xs)*1.04)))
        ax.grid(alpha=.25)
    axes[0].set_ylabel('Cumulative fraction of configurations')
    fig.suptitle('Paired improvement distribution — 100 cases × 4 core counts')
    fig.savefig(run/'reports/v3_v4_reduction.png',dpi=180)
    plt.close(fig)
    print(table,flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    compare(parser.parse_args().run)
