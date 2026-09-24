"""Solve one official graph and write the exact two-field contest plan."""
from __future__ import annotations

import argparse
from pathlib import Path

from official import DATA, atomic_json, read_json, settings
from run import search


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", type=Path)
    parser.add_argument("--cores", type=int, required=True, choices=(2, 3, 4, 5))
    parser.add_argument("--scene", required=True, choices=("A", "B", "L2"))
    parser.add_argument("--budget", type=int, default=72)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.budget < 1:
        parser.error("budget must be positive")
    if not (DATA / "config.txt").is_file():
        parser.error("official data/config.txt is missing")
    graph = read_json(args.graph)
    best, _, _, _, calls = search(graph, args.cores, args.scene, settings(), args.budget)
    atomic_json(args.output, best[2])
    print(f"plan={args.output.resolve()} makespan={best[3]['makespan']} "
          f"added_copy_bytes={best[3]['added_copy_bytes']} official_evaluations={calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
