"""Export complete npu_5 plans under the official per-case filename."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from run import FOLDERS, validate
from worker import read_json


def export(run, destination):
    manifest = read_json(run / "manifest.json")
    if manifest.get("status") != "complete":
        raise ValueError("run is not complete")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("destination must be empty to avoid mixing runs")
    audit = validate(run)
    if audit["status"] != "ok":
        raise ValueError(f"run validation failed: {audit['issues'][:5]}")
    for scene, folder in FOLDERS.items():
        for cores in manifest["cores"]:
            target_dir = destination / folder / f"{cores}core"
            target_dir.mkdir(parents=True, exist_ok=True)
            for case in manifest["cases"]:
                source = run / folder / "plans" / f"{case}_{cores}core.json"
                plan = read_json(source)
                if set(plan) != {"node_to_subgraph", "core_schedules"}:
                    raise ValueError(f"wrong plan shape: {source}")
                shutil.copy2(source, target_dir / f"{case}_multicore_res.json")
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    print(export(args.run.resolve(), args.destination.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
