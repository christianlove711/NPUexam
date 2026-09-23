"""Small official-evaluator integration checks; not a full benchmark."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from run import FOLDERS, V4_RUN, canonical_hash, optimize, summarize, validate  # noqa: E402
from export import export  # noqa: E402
from worker import read_json, run_singlecore, score  # noqa: E402


class OfficialContractTests(unittest.TestCase):
    def test_three_scenes_pairing_and_nonregression(self):
        with tempfile.TemporaryDirectory(prefix="npu5_test_") as directory:
            run = Path(directory)
            (run / "manifest.json").write_text(
                json.dumps({"cases": ["case_019"], "cores": [2]}),
                encoding="utf-8")
            run_singlecore(str(run), "case_019")
            optimize(str(run), "case_019", 2, 8, 8)
            failures, _ = summarize(run, ["case_019"], [2])
            self.assertFalse(failures)
            self.assertEqual(validate(run)["status"], "ok")
            for scene, folder in FOLDERS.items():
                job = read_json(run / folder / "jobs" / "case_019_2core.json")
                seed = read_json(V4_RUN / folder / "jobs" / "case_019_2core.json")
                self.assertLessEqual(score(job["selected"]), score(seed["selected"]))
            b = read_json(run / "problem_2" / "jobs" / "case_019_2core.json")
            l2 = read_json(run / "problem_3" / "jobs" / "case_019_2core.json")
            b_plan = read_json(run / "problem_2" / "plans" / "case_019_2core.json")
            self.assertEqual(l2["b_plan_hash"], canonical_hash(b_plan))
            self.assertEqual(l2["paired"]["no_l2"]["makespan"],
                             b["selected"]["makespan"])
            self.assertEqual(optimize(str(run), "case_019", 2, 8, 8)[2],
                             "cached")
            (run / "manifest.json").write_text(
                json.dumps({"cases": ["case_019"], "cores": [2],
                            "status": "complete"}), encoding="utf-8")
            destination = run / "submission"
            export(run, destination)
            for folder in FOLDERS.values():
                plan = read_json(destination / folder / "2core" /
                                 "case_019_multicore_res.json")
                self.assertEqual(set(plan),
                                 {"node_to_subgraph", "core_schedules"})


if __name__ == "__main__":
    unittest.main()
