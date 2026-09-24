"""Small official-evaluator and isolation tests; never run the full suite."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
sys.path.insert(0, str(ROOT))

from official import DATA, read_json, canonical_hash, score  # noqa: E402
from run import FOLDERS, optimize, singlecore, summarize, validate  # noqa: E402
from export import export  # noqa: E402


class OfficialContractTests(unittest.TestCase):
    def test_problem_one_only_runner(self):
        with tempfile.TemporaryDirectory(prefix="npu5_a_only_") as directory:
            result = subprocess.run(
                [sys.executable, str(ROOT / "run.py"), "--scene-a-only",
                 "--cases", "case_019", "--cores", "2", "--workers", "1",
                 "--small-budget", "8", "--large-budget", "8", "--no-plots",
                 "--output-root", directory],
                cwd=PROJECT, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            runs = list(Path(directory).iterdir())
            self.assertEqual(len(runs), 1)
            run = runs[0]
            self.assertEqual(read_json(run / "manifest.json")["scenes"], ["A"])
            self.assertTrue((run / "problem_1" / "summary.csv").is_file())
            self.assertFalse((run / "problem_2").exists())
            self.assertFalse((run / "problem_3").exists())
            self.assertEqual(validate(run, replay=True)["status"], "ok")

    def test_three_scenes_pairing_and_resume(self):
        with tempfile.TemporaryDirectory(prefix="npu5_test_") as directory:
            run = Path(directory)
            (run / "manifest.json").write_text(
                json.dumps({"cases": ["case_019"], "cores": [2], "status": "complete"}),
                encoding="utf-8")
            self.assertEqual(singlecore(run, "case_019"), "computed")
            self.assertEqual(optimize(run, "case_019", 2, 8, 8), "computed")
            failures, summaries = summarize(run, ["case_019"], [2])
            self.assertFalse(failures)
            self.assertEqual(validate(run, replay=True)["status"], "ok")
            self.assertEqual(optimize(run, "case_019", 2, 8, 8), "cached")
            self.assertEqual(singlecore(run, "case_019"), "cached")
            self.assertEqual({scene: len(rows) for scene, rows in summaries.items()},
                             {"A": 1, "B": 1, "L2": 1})
            b = read_json(run / "problem_2" / "jobs" / "case_019_2core.json")
            l2 = read_json(run / "problem_3" / "jobs" / "case_019_2core.json")
            b_plan = read_json(run / "problem_2" / "plans" / "case_019_2core.json")
            self.assertEqual(l2["b_plan_hash"], canonical_hash(b_plan))
            self.assertEqual(score(l2["paired"]["no_l2"]), score(b["selected"]))
            self.assertLessEqual(score(l2["selected"]), score(l2["paired"]["with_l2"]))
            destination = run / "submission"
            export(run, destination)
            for folder in FOLDERS.values():
                plan = read_json(destination / folder / "2core" /
                                 "case_019_multicore_res.json")
                self.assertEqual(set(plan), {"node_to_subgraph", "core_schedules"})

    def test_isolated_single_graph(self):
        with tempfile.TemporaryDirectory(prefix="npu5_isolated_") as directory:
            isolated = Path(directory)
            shutil.copytree(ROOT, isolated / "npu_5", ignore=shutil.ignore_patterns(
                "__pycache__", "runs", "targeted_runs", "diagnostics",
                "tests", "*.pyc"))
            shutil.copytree(PROJECT / "code", isolated / "code", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (isolated / "data").mkdir()
            shutil.copy2(DATA / "config.txt", isolated / "data" / "config.txt")
            shutil.copy2(DATA / "case_019.json", isolated / "data" / "case_019.json")
            output = isolated / "plan.json"
            result = subprocess.run(
                [sys.executable, str(isolated / "npu_5" / "solve.py"),
                 str(isolated / "data" / "case_019.json"), "--cores", "2",
                 "--scene", "A", "--budget", "4", "--output", str(output)],
                cwd=isolated, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(set(read_json(output)), {"node_to_subgraph", "core_schedules"})


if __name__ == "__main__":
    unittest.main()
