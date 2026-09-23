"""静态检查清单范围内的结果覆盖、第一版回退与 B/L2 配对一致性。

此脚本不重新模拟方案；通过不代表全局最优，也不自动证明覆盖全部 100 例。
正式全量还需确认 manifest 的 cases/cores 和各问题汇总行数。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(run, require_plots=True):
    """返回发现的问题字符串列表；空列表表示本脚本检查的项目均通过。"""
    run = Path(run)
    manifest = _read(run / "manifest.json")
    problems = {"A": "problem_1", "B": "problem_2", "L2": "problem_3"}
    problems_to_check = []
    for case in manifest["cases"]:
        baseline = run / "baseline" / "jobs" / f"{case}.json"
        if not baseline.is_file():
            problems_to_check.append(f"missing singlecore {case}")
            continue
        baseline_row = _read(baseline)
        if baseline_row.get("status") != "ok" or baseline_row.get("makespan", 0) <= 0:
            problems_to_check.append(f"invalid singlecore {case}")
        for cores in manifest["cores"]:
            rows = {}
            for scene, folder in problems.items():
                job = run / folder / "jobs" / f"{case}_{cores}core.json"
                plan = run / folder / "plans" / f"{case}_{cores}core.json"
                if not job.is_file() or not plan.is_file():
                    problems_to_check.append(f"missing {scene} {case} {cores}")
                    continue
                row = _read(job)
                rows[scene] = row
                selected = row.get("selected") or {}
                if row.get("status") != "ok" or selected.get("makespan", 0) <= 0:
                    problems_to_check.append(f"invalid {scene} {case} {cores}")
                    continue
                v1 = row.get("v1")
                if scene in ("A", "B") and not v1:
                    problems_to_check.append(f"missing v1 comparison {scene} {case} {cores}")
                if v1 and selected["makespan"] > v1["makespan"]:
                    problems_to_check.append(f"worse than v1 {scene} {case} {cores}")
            if "B" in rows and "L2" in rows:
                b, l2 = rows["B"], rows["L2"]
                b_plan = run / "problem_2" / "plans" / f"{case}_{cores}core.json"
                paired = l2.get("paired") or {}
                no_cache = paired.get("no_l2") or {}
                with_cache = paired.get("with_l2") or {}
                if l2.get("b_plan_sha256") != _hash(b_plan):
                    problems_to_check.append(f"stale L2 pair {case} {cores}")
                if no_cache.get("makespan") != (b.get("selected") or {}).get("makespan"):
                    problems_to_check.append(f"B/L2 pair mismatch {case} {cores}")
                if (with_cache.get("makespan") and l2.get("selected") and
                        l2["selected"]["makespan"] > with_cache["makespan"]):
                    problems_to_check.append(f"L2 selected worse than paired {case} {cores}")
    if require_plots:
        for name in ("problem_1_speedup.png", "problem_1_vs_v1.png",
                     "problem_2_speedup.png", "problem_2_vs_v1.png",
                     "problem_3_paired_curves.png", "problem_3_cache_metrics.png"):
            if not (run / "reports" / name).is_file():
                problems_to_check.append(f"missing chart {name}")
    return problems_to_check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args()
    issues = validate(args.run, require_plots=not args.no_plots)
    for issue in issues:
        print(issue)
    print(f"validation: {'FAIL' if issues else 'OK'} ({len(issues)} issues)")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
