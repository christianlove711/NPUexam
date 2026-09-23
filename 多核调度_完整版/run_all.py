"""三问实验的一键入口：建立独立运行目录、调度 worker、汇总并绘图。

路径以本文件为基准；--workers 控制电脑并发进程数，--cores 控制模拟 NPU 核数。
必需评估先完成，追加搜索才受 --search-hours 的提交预算限制。
参数、续跑边界与退出状态见《运行与环境说明.md》。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ATTACHMENT = ROOT.parent
DATA = ATTACHMENT / "data"
SCENE_DIR = {"A": "problem_1", "B": "problem_2", "L2": "problem_3"}
ACTIVE_RUN = None


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def _load(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def _case_names(args):
    available = sorted(path.stem for path in DATA.glob("case_*.json"))
    if len(available) != 100:
        raise RuntimeError(f"Expected 100 official cases, found {len(available)}")
    if args.cases:
        missing = set(args.cases) - set(available)
        if missing:
            raise ValueError(f"Unknown cases: {sorted(missing)}")
        return sorted(set(args.cases))
    if args.quick:
        return [case for case in ("case_001", "case_019", "case_035")
                if case in available]
    return available


def _create_or_resume(args, cases, cores):
    """新建时间戳目录或检查续跑清单；续跑不校验求解器源码和图数据哈希。"""
    config_hash = _sha256(DATA / "config.txt")
    if args.resume:
        run = args.resume.resolve()
        manifest = _load(run / "manifest.json")
        expected = {"cases": cases, "cores": cores,
                    "config_sha256": config_hash}
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise ValueError(f"Resume mismatch for {key}: {manifest.get(key)} != {value}")
        manifest["status"] = "running"
        manifest["last_resumed_at"] = datetime.now().astimezone().isoformat()
        manifest["workers"] = args.workers
        manifest["search_hours"] = args.search_hours
        manifest["timeout_minutes"] = args.timeout_minutes
        _write_json(run / "manifest.json", manifest)
        return run, manifest
    run = ROOT / "runs" / (datetime.now().strftime("%Y%m%d_%H%M%S")
                           + "_" + secrets.token_hex(3))
    run.mkdir(parents=True, exist_ok=False)
    for name in ("baseline", "problem_1", "problem_2", "problem_3", "reports"):
        (run / name).mkdir()
    manifest = {"created_at": datetime.now().astimezone().isoformat(),
                "cases": cases, "cores": cores, "quick": args.quick,
                "workers": args.workers, "search_hours": args.search_hours,
                "timeout_minutes": args.timeout_minutes,
                "seed": 2026, "config_sha256": config_hash,
                "python": sys.version, "status": "running"}
    _write_json(run / "manifest.json", manifest)
    return run, manifest


def _job_file(run, phase, case, cores=None, scene=None):
    if phase == "singlecore":
        return run / "baseline" / "jobs" / f"{case}.json"
    folder = run / SCENE_DIR[scene]
    subfolder = "search_jobs" if phase == "search" else "jobs"
    return folder / subfolder / f"{case}_{cores}core.json"


def _invoke(run, phase, case, cores, scene, timeout_minutes):
    command = [sys.executable, str(ROOT / "worker.py"),
               "--run", str(run), "--case", case, "--phase", phase]
    if cores is not None:
        command += ["--cores", str(cores)]
    if scene is not None:
        command += ["--scene", scene]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=timeout_minutes * 60, check=False)
        return {"case": case, "cores": cores, "scene": scene,
                "phase": phase, "returncode": result.returncode,
                "output": (result.stdout + result.stderr)[-6000:]}
    except subprocess.TimeoutExpired:
        return {"case": case, "cores": cores, "scene": scene,
                "phase": phase, "returncode": 124,
                "output": f"Timed out after {timeout_minutes} minutes"}


def _run_phase(run, phase, jobs, workers, timeout_minutes, retry_failed=False,
               deadline=None):
    """执行一阶段任务，跳过可复用结果；B 方案改变后重算过期的 L2 配对。

deadline 限制新任务提交，已运行的子进程仍等待完成或触发单任务超时。
返回进程失败记录；必需结果是否齐全由后续汇总检查。
    """
    pending = []
    for case, cores, scene in jobs:
        path = _job_file(run, phase, case, cores, scene)
        if path.is_file():
            try:
                row = _load(path)
                stale = False
                if scene == "L2" and phase in ("required", "search"):
                    b_path = run / "problem_2" / "plans" / f"{case}_{cores}core.json"
                    stale = (b_path.is_file() and
                             row.get("b_plan_sha256") != _sha256(b_path))
                if not stale and (row.get("status", "ok") == "ok"
                                  or not retry_failed):
                    continue
            except (OSError, ValueError):
                pass
        pending.append((case, cores, scene))
    print(f"{phase}: {len(pending)} pending of {len(jobs)}", flush=True)
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        active = {}
        iterator = iter(pending)

        def submit_more():
            while len(active) < workers:
                if deadline is not None and time.monotonic() >= deadline:
                    return
                try:
                    case, cores, scene = next(iterator)
                except StopIteration:
                    return
                future = pool.submit(_invoke, run, phase, case, cores,
                                     scene, timeout_minutes)
                active[future] = (case, cores, scene)

        submit_more()
        completed = 0
        while active:
            done, _ = concurrent.futures.wait(
                active, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                case, cores, scene = active.pop(future)
                result = future.result()
                completed += 1
                if result["returncode"]:
                    failures.append(result)
                    print(f"FAILED {phase} {case} {scene} {cores}: "
                          f"{result['output'][-400:]}", flush=True)
                elif completed <= 3 or completed % 10 == 0 or completed == len(pending):
                    print(f"{phase}: {completed}/{len(pending)} completed", flush=True)
            submit_more()
    return failures


def _csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def summarize(run, cases, cores):
    """从已保存的 job JSON 重建逐例 CSV；缺失或失败项单独列入失败表。"""
    baseline = []
    errors = []
    for case in cases:
        path = _job_file(run, "singlecore", case)
        if path.is_file():
            row = _load(path)
            if row.get("status") == "ok":
                baseline.append(row)
            else:
                errors.append({"phase": "singlecore", "case": case,
                               "cores": "", "error": str(row.get("error", "failed"))})
        else:
            errors.append({"phase": "singlecore", "case": case,
                           "cores": "", "error": "missing result"})
    _csv(run / "baseline" / "singlecore.csv", baseline,
         ["case", "status", "makespan", "added_copy_bytes",
          "paired_no_l2_makespan", "paired_with_l2_makespan",
          "paired_no_l2_added_copy_bytes",
          "paired_with_l2_added_copy_bytes",
          "paired_l2_speedup", "paired_cache_hit_rate", "evaluation_seconds"])
    one_core = {row["case"]: row["makespan"] for row in baseline}
    summaries = {}
    for scene, folder in SCENE_DIR.items():
        rows = []
        for case in cases:
            for count in cores:
                path = _job_file(run, "required", case, count, scene)
                if not path.is_file():
                    errors.append({"phase": scene, "case": case,
                                   "cores": count, "error": "missing result"})
                    continue
                job = _load(path)
                if job.get("status") != "ok":
                    errors.append({"phase": scene, "case": case, "cores": count,
                                   "error": str(job.get("errors", "failed"))})
                    continue
                selected = job["selected"]
                v1 = job.get("v1") or {}
                paired = job.get("paired") or {}
                no_l2 = paired.get("no_l2") or {}
                with_l2 = paired.get("with_l2") or {}
                base = one_core.get(case)
                row = {
                    "case": case, "cores": count, "status": "ok",
                    "singlecore_makespan": base,
                    "makespan": selected["makespan"],
                    "speedup": base / selected["makespan"] if base else "",
                    "added_copy_bytes": selected["added_copy_bytes"],
                    "v1_makespan": v1.get("makespan", ""),
                    "gain_over_v1": (v1["makespan"] / selected["makespan"]
                                     if v1 else ""),
                    "selected_source": job["selected_source"],
                    "generation_seconds": job["generation_seconds"],
                    "total_seconds": job["total_seconds"],
                    "paired_no_l2_makespan": no_l2.get("makespan", ""),
                    "paired_with_l2_makespan": with_l2.get("makespan", ""),
                    "paired_no_l2_added_copy_bytes": no_l2.get("added_copy_bytes", ""),
                    "paired_with_l2_added_copy_bytes": with_l2.get("added_copy_bytes", ""),
                    "paired_l2_speedup": paired.get("cache_speedup", ""),
                    "paired_cache_hit_rate": with_l2.get("cache_hit_rate", ""),
                    "cache_hit_rate": selected.get("cache_hit_rate", ""),
                }
                rows.append(row)
        fields = list(rows[0]) if rows else [
            "case", "cores", "status", "singlecore_makespan", "makespan",
            "speedup", "added_copy_bytes", "v1_makespan", "gain_over_v1",
            "selected_source", "generation_seconds", "total_seconds",
            "paired_no_l2_makespan", "paired_with_l2_makespan",
            "paired_no_l2_added_copy_bytes",
            "paired_with_l2_added_copy_bytes",
            "paired_l2_speedup", "paired_cache_hit_rate", "cache_hit_rate"]
        _csv(run / folder / "summary.csv", rows, fields)
        summaries[scene] = rows
    _csv(run / "reports" / "failures.csv", errors,
         ["phase", "case", "cores", "error"])
    _aggregate(run, summaries, cases, cores, one_core, baseline)
    return errors, summaries


def _aggregate(run, summaries, cases, cores, one_core, baseline):
    """按场景和核数汇总：先逐例计算比值，再等权平均，不能改为时间总和之比。"""
    rows = []
    for scene, items in summaries.items():
        for count in [1] + cores:
            matching = [row for row in items if row["cores"] == count]
            if count == 1:
                avg_speedup = 1.0 if len(one_core) == len(cases) else ""
                paired_one = ([row["paired_l2_speedup"] for row in baseline]
                              if scene == "L2" else [])
                rows.append({"scene": scene, "cores": 1, "count": len(one_core),
                             "mean_speedup": avg_speedup,
                             "mean_gain_over_v1": "", "win_rate_over_v1": "",
                             "mean_paired_l2_speedup": (sum(paired_one) / len(paired_one)
                                                       if paired_one else "")})
                continue
            speeds = [float(row["speedup"]) for row in matching
                      if row["speedup"] != ""]
            gains = [float(row["gain_over_v1"]) for row in matching
                     if row["gain_over_v1"] != ""]
            paired = [float(row["paired_l2_speedup"]) for row in matching
                      if row["paired_l2_speedup"] != ""]
            wins = [row for row in matching if row["v1_makespan"] != ""
                    and row["makespan"] < row["v1_makespan"]]
            rows.append({"scene": scene, "cores": count,
                         "count": len(matching),
                         "mean_speedup": sum(speeds) / len(speeds) if speeds else "",
                         "mean_gain_over_v1": sum(gains) / len(gains) if gains else "",
                         "win_rate_over_v1": len(wins) / len(gains) if gains else "",
                         "mean_paired_l2_speedup": (sum(paired) / len(paired)
                                                   if paired else "")})
    _csv(run / "reports" / "aggregate.csv", rows,
         ["scene", "cores", "count", "mean_speedup",
          "mean_gain_over_v1", "win_rate_over_v1", "mean_paired_l2_speedup"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="run three representative cases and 2/4 cores")
    parser.add_argument("--cases", nargs="+", help="selected case stems")
    parser.add_argument("--cores", nargs="+", type=int)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--search-hours", type=float)
    parser.add_argument("--timeout-minutes", type=int)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args(argv)
    prior = _load(args.resume.resolve() / "manifest.json") if args.resume else None
    if args.workers is None:
        args.workers = (prior["workers"] if prior else
                        min(4, max(1, (os.cpu_count() or 2) // 2)))
    if args.search_hours is None:
        args.search_hours = prior["search_hours"] if prior else 10.0
    if args.timeout_minutes is None:
        args.timeout_minutes = prior["timeout_minutes"] if prior else 30
    if args.workers < 1 or args.search_hours < 0 or args.timeout_minutes < 1:
        parser.error("workers and timeout must be positive; search hours non-negative")
    cores = args.cores or (prior["cores"] if prior else
                           ([2, 4] if args.quick else [2, 3, 4, 5]))
    if sorted(set(cores)) != sorted(cores) or any(not 2 <= n <= 5 for n in cores):
        parser.error("cores must be distinct values from 2 to 5")
    cases = (prior["cases"] if prior and not args.cases and not args.quick
             else _case_names(args))
    run, manifest = _create_or_resume(args, cases, cores)
    global ACTIVE_RUN
    ACTIVE_RUN = run
    print(f"run directory: {run}", flush=True)
    failures = []
    failures += _run_phase(run, "singlecore",
                           [(case, None, None) for case in cases],
                           args.workers, args.timeout_minutes, args.retry_failed)
    for scene in ("A", "B", "L2"):
        jobs = [(case, count, scene) for case in cases for count in cores]
        failures += _run_phase(run, "required", jobs,
                               args.workers, args.timeout_minutes,
                               args.retry_failed)
    errors, _ = summarize(run, cases, cores)
    if not errors and args.search_hours and not args.quick:
        deadline = time.monotonic() + args.search_hours * 3600
        for scene in ("A", "B", "L2"):
            jobs = [(case, count, scene) for case in cases for count in cores]
            failures += _run_phase(run, "search", jobs,
                                   args.workers, args.timeout_minutes,
                                   args.retry_failed, deadline=deadline)
            if scene == "B":
                l2_jobs = [(case, count, "L2") for case in cases for count in cores]
                failures += _run_phase(run, "required", l2_jobs,
                                       args.workers, args.timeout_minutes,
                                       args.retry_failed)
            if time.monotonic() >= deadline:
                break
        errors, _ = summarize(run, cases, cores)
    if not args.no_plots and not errors:
        try:
            from reports import build_plots
            build_plots(run)
        except ImportError as exc:
            errors.append({"phase": "plots", "case": "", "cores": "",
                           "error": f"Matplotlib is required: {exc}"})
            print("Install requirements.txt to generate PNG charts", flush=True)
        except Exception as exc:
            errors.append({"phase": "plots", "case": "", "cores": "",
                           "error": f"{type(exc).__name__}: {exc}"})
            print(f"Plot generation failed: {exc}", flush=True)
    if not errors:
        from verify_run import validate
        validation_issues = validate(run, require_plots=not args.no_plots)
        _write_json(run / "reports" / "validation.json",
                    {"status": "ok" if not validation_issues else "error",
                     "issues": validation_issues})
        errors.extend({"phase": "validation", "case": "", "cores": "",
                       "error": issue} for issue in validation_issues)
    if errors:
        _csv(run / "reports" / "failures.csv", errors,
             ["phase", "case", "cores", "error"])
    if failures:
        _csv(run / "reports" / "worker_failures.csv", failures,
             ["case", "cores", "scene", "phase", "returncode", "output"])
    manifest["status"] = "complete" if not errors else "incomplete"
    manifest["finished_at"] = datetime.now().astimezone().isoformat()
    manifest["missing_or_failed_count"] = len(errors)
    manifest["worker_failure_count"] = len(failures)
    manifest["search_worker_failure_count"] = sum(
        failure["phase"] == "search" for failure in failures)
    _write_json(run / "manifest.json", manifest)
    print(f"status={manifest['status']} missing_or_failed={len(errors)} run={run}",
          flush=True)
    return 0 if not errors else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        if ACTIVE_RUN is not None:
            manifest_path = ACTIVE_RUN / "manifest.json"
            current = _load(manifest_path)
            current["status"] = "interrupted"
            current["interrupted_at"] = datetime.now().astimezone().isoformat()
            _write_json(manifest_path, current)
            print(f"Interrupted. Resume with --resume {ACTIVE_RUN}", flush=True)
        raise SystemExit(130)
