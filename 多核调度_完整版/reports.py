"""读取已保存 CSV，以无界面 Matplotlib 生成六张结果图，不重新运行评估器。

每张图的坐标、公式、数据字段与示例见《结果字段与图表解读.md》。
均值采用用例等权口径；未运行的核数即使有刻度或连线也不代表实测点。
"""

from __future__ import annotations

import csv
import os
from collections import defaultdict
from pathlib import Path


def _read(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def build_plots(run):
    """重建指定运行目录的 PNG；依赖 aggregate、三问 summary 和 singlecore CSV。"""
    run = Path(run)
    cache_dir = run / "reports" / "matplotlib_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    reports = run / "reports"
    reports.mkdir(exist_ok=True)
    plt.rcParams.update({"figure.dpi": 150, "savefig.dpi": 180,
                         "axes.grid": True, "grid.alpha": 0.25})
    aggregate = _read(reports / "aggregate.csv")
    for scene, folder in (("A", "problem_1"), ("B", "problem_2")):
        rows = sorted((row for row in aggregate if row["scene"] == scene
                       and row["mean_speedup"]), key=lambda row: int(row["cores"]))
        fig, ax = plt.subplots(figsize=(7, 4.5))
        ax.plot([int(row["cores"]) for row in rows],
                [float(row["mean_speedup"]) for row in rows], "o-",
                linewidth=2, label="Version 2 selected")
        ax.plot([1, 5], [1, 5], "--", color="gray", linewidth=1,
                label="Ideal linear speedup")
        ax.set(title=f"Problem {1 if scene == 'A' else 2}: mean speedup",
               xlabel="Core count", ylabel="Mean of per-case speedups")
        ax.set_xticks([1, 2, 3, 4, 5])
        ax.legend()
        fig.tight_layout()
        fig.savefig(reports / f"problem_{1 if scene == 'A' else 2}_speedup.png")
        plt.close(fig)

        detail = _read(run / folder / "summary.csv")
        counts = sorted(set(int(row["cores"]) for row in detail))
        wins = []
        mean_gain = []
        for count in counts:
            group = [row for row in detail if int(row["cores"]) == count
                     and row["v1_makespan"]]
            wins.append(sum(float(row["makespan"]) < float(row["v1_makespan"])
                        for row in group) / len(group) if group else 0)
            mean_gain.append(sum(float(row["gain_over_v1"]) for row in group)
                             / len(group) if group else 0)
        fig, axes = plt.subplots(1, 2, figsize=(10, 4))
        axes[0].bar(counts, wins)
        axes[0].set(title="Win rate over version 1", xlabel="Core count",
                    ylabel="Fraction of cases", ylim=(0, 1))
        axes[1].plot(counts, mean_gain, "o-")
        axes[1].axhline(1, color="gray", linestyle="--")
        axes[1].set(title="Mean V1 / selected makespan", xlabel="Core count",
                    ylabel="Ratio")
        fig.tight_layout()
        fig.savefig(reports / f"problem_{1 if scene == 'A' else 2}_vs_v1.png")
        plt.close(fig)

    # 题三两张图只展示同一 B 方案的配对；不使用另行优化的 L2 selected 成绩。
    p3 = _read(run / "problem_3" / "summary.csv")
    one = _read(run / "baseline" / "singlecore.csv")
    groups = defaultdict(list)
    for row in p3:
        if row["paired_no_l2_makespan"] and row["paired_with_l2_makespan"]:
            groups[int(row["cores"])].append(row)
    counts = [1] + sorted(groups)
    no_l2_speedup = [sum(float(row["makespan"]) /
                         float(row["paired_no_l2_makespan"]) for row in one) / len(one)]
    with_l2_speedup = [sum(float(row["makespan"]) /
                           float(row["paired_with_l2_makespan"]) for row in one) / len(one)]
    cache_gain = [sum(float(row["paired_l2_speedup"]) for row in one) / len(one)]
    hit_rates = [sum(float(row["paired_cache_hit_rate"]) for row in one) / len(one)]
    moved_no_l2 = [sum(float(row.get("paired_no_l2_added_copy_bytes")
                             or row["added_copy_bytes"])
                       for row in one) / len(one)]
    moved_with_l2 = [sum(float(row.get("paired_with_l2_added_copy_bytes")
                               or row["added_copy_bytes"])
                         for row in one) / len(one)]
    for count in counts[1:]:
        rows = groups[count]
        no_l2_speedup.append(sum(float(row["singlecore_makespan"])
                                 / float(row["paired_no_l2_makespan"])
                                 for row in rows) / len(rows))
        with_l2_speedup.append(sum(float(row["singlecore_makespan"])
                                   / float(row["paired_with_l2_makespan"])
                                   for row in rows) / len(rows))
        cache_gain.append(sum(float(row["paired_l2_speedup"])
                              for row in rows) / len(rows))
        hit_rates.append(sum(float(row["paired_cache_hit_rate"])
                             for row in rows) / len(rows))
        moved_no_l2.append(sum(float(row["paired_no_l2_added_copy_bytes"])
                               for row in rows) / len(rows))
        moved_with_l2.append(sum(float(row["paired_with_l2_added_copy_bytes"])
                                 for row in rows) / len(rows))
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.plot(counts, no_l2_speedup, "o-", label="No L2, same B plan")
    ax.plot(counts, with_l2_speedup, "s-", label="Read-only L2, same B plan")
    ax.set(title="Problem 3: paired L2 comparison", xlabel="Core count",
           ylabel="Mean speedup vs one core")
    ax.set_xticks([1, 2, 3, 4, 5])
    ax.legend()
    fig.tight_layout()
    fig.savefig(reports / "problem_3_paired_curves.png")
    plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(13, 4))
    axes[0].plot(counts, cache_gain, "o-")
    axes[0].axhline(1, color="gray", linestyle="--")
    axes[0].set(title="No L2 / L2 makespan", xlabel="Core count", ylabel="Ratio")
    axes[1].plot(counts, hit_rates, "o-")
    axes[1].set(title="Mean byte hit rate", xlabel="Core count", ylabel="Fraction")
    axes[2].plot(counts, moved_no_l2, "o-", label="No L2")
    axes[2].plot(counts, moved_with_l2, "s-", label="With L2")
    # 标题中的 DDR bytes 是官方 added_copy_bytes 计数，未扣除 Cache 命中。
    axes[2].set(title="Mean added DDR bytes", xlabel="Core count", ylabel="Bytes")
    axes[2].legend()
    fig.tight_layout()
    fig.savefig(reports / "problem_3_cache_metrics.png")
    plt.close(fig)
