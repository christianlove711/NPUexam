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
