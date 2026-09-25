"""Plots directly from fifth-version CSVs; no evaluation occurs here."""
from __future__ import annotations

import csv
import os
from pathlib import Path


def rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def build_plots(run):
    run = Path(run)
    cache = run / "reports" / "matplotlib_cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    aggregate = rows(run / "reports" / "aggregate.csv")
    for scene, number in (("A", 1), ("B", 2), ("L2", 3)):
        relevant = sorted((r for r in aggregate if r["scene"] == scene and r["mean_speedup"]),
                          key=lambda r: int(r["cores"]))
        if not relevant:
            continue
        fig, ax = plt.subplots(figsize=(7, 4.5))
        ax.plot([int(r["cores"]) for r in relevant],
                [float(r["mean_speedup"]) for r in relevant], "o-", label="Selected plan")
        ax.set(xlabel="NPU cores", ylabel="Mean per-case speedup",
               title=f"Problem {number}: speedup")
        ax.set_xticks([1, 2, 3, 4, 5])
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(run / "reports" / f"problem_{number}_speedup.png", dpi=180)
        plt.close(fig)
    if any(r['scene'] == 'L2' for r in aggregate):
        # Match case populations across all three curves, including singlecore.
        import json
        manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
        cases = set(manifest.get('scene_cases', {}).get('L2', manifest['cases']))
        base = {r['case']: r for r in rows(run / 'baseline/singlecore.csv') if r['case'] in cases}
        selected = rows(run / 'problem_3/summary.csv')
        points = []
        for count in [1] + manifest['cores']:
            if count == 1:
                samples = [(float(r['makespan']), float(r['paired_no_l2_makespan']),
                            float(r['paired_with_l2_makespan']), float(r['paired_with_l2_makespan']))
                           for r in base.values()]
            else:
                samples = [(float(base[r['case']]['makespan']), float(r['paired_no_l2_makespan']),
                            float(r['paired_with_l2_makespan']), float(r['makespan']))
                           for r in selected if int(r['cores']) == count]
            if samples:
                points.append({'cores': count, 'count': len(samples),
                               'no_l2': sum(s[0]/s[1] for s in samples)/len(samples),
                               'paired_l2': sum(s[0]/s[2] for s in samples)/len(samples),
                               'optimized_l2': sum(s[0]/s[3] for s in samples)/len(samples)})
        with (run / 'reports/problem_3_comparison.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['cores', 'count', 'no_l2', 'paired_l2', 'optimized_l2'])
            writer.writeheader()
            writer.writerows(points)
        fig, ax = plt.subplots(figsize=(7, 4.5))
        for field, label in [('no_l2', 'B without L2'), ('paired_l2', 'Same B plan with L2'),
                             ('optimized_l2', 'Optimized L2 plan')]:
            ax.plot([r['cores'] for r in points], [r[field] for r in points], 'o-', label=label)
        ax.set(xlabel='NPU cores', ylabel='Mean singlecore / Makespan',
               title=f'Problem 3: matched {len(cases)} cases')
        ax.set_xticks([1, 2, 3, 4, 5])
        ax.grid(alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(run / 'reports/problem_3_comparison.png', dpi=180)
        plt.close(fig)
    relevant = sorted((r for r in aggregate if r["scene"] == "L2" and
                       r["mean_paired_l2_speedup"]), key=lambda r: int(r["cores"]))
    if relevant:
        fig, ax = plt.subplots(figsize=(7, 4.5))
        ax.plot([int(r["cores"]) for r in relevant],
                [float(r["mean_paired_l2_speedup"]) for r in relevant], "o-")
        ax.set(xlabel="NPU cores", ylabel="Mean no-L2 / with-L2 Makespan",
               title="Problem 3: same-plan Cache gain")
        ax.set_xticks([1, 2, 3, 4, 5])
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(run / "reports" / "problem_3_cache_gain.png", dpi=180)
        plt.close(fig)
