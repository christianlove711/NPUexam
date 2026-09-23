import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import run_all  # noqa: E402
from solver_v2 import ATTACHMENT, generate_candidates, legacy_plan  # noqa: E402
from solver_idblocks import generate_idblock_candidates  # noqa: E402
from stub_multicore_cut_and_schedule import derive_multicore_plan  # noqa: E402
from worker import _neighbors, official_evaluate, settings  # noqa: E402


class SolverContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = json.loads((ATTACHMENT / "data" / "case_019.json")
                               .read_text(encoding="utf-8"))
        cls.config = settings()

    def test_candidates_are_officially_valid_for_all_scenes(self):
        for scene in ("A", "B", "L2"):
            with self.subTest(scene=scene):
                candidates = generate_candidates(self.graph, 2, scene,
                                                 self.config, limit=2)
                self.assertTrue(candidates)
                for _, plan, _ in candidates:
                    view = derive_multicore_plan(self.graph, plan)
                    self.assertEqual(view["num_cores"], 2)

    def test_selected_pool_contains_first_version_fallback(self):
        plan = legacy_plan(self.graph, 2, "A")
        baseline = official_evaluate(self.graph, plan, "A", self.config)
        pool = [baseline]
        for _, candidate, _ in generate_candidates(self.graph, 2, "A",
                                                   self.config, limit=2):
            try:
                pool.append(official_evaluate(self.graph, candidate,
                                              "A", self.config))
            except Exception:
                pass
        best = min(pool, key=lambda row: (row["makespan"],
                                          row["added_copy_bytes"]))
        self.assertLessEqual(best["makespan"], baseline["makespan"])

    def test_l2_pair_evaluates_the_same_plan(self):
        plan = legacy_plan(self.graph, 2, "B")
        no_cache = official_evaluate(self.graph, plan, "B", self.config)
        cache = official_evaluate(self.graph, plan, "L2", self.config)
        self.assertGreater(no_cache["makespan"], 0)
        self.assertGreater(cache["makespan"], 0)
        self.assertGreaterEqual(cache["cache_hit_rate"], 0)

    def test_search_neighbors_keep_official_plan_contract(self):
        plan = legacy_plan(self.graph, 2, "B")
        neighbors = _neighbors(self.graph, plan)
        self.assertTrue(neighbors)
        for _, candidate in neighbors:
            derive_multicore_plan(self.graph, candidate)

    def test_idblock_candidates_cover_all_cores_and_scenes(self):
        for cores in (2, 3, 4, 5):
            for scene in ("A", "B", "L2"):
                with self.subTest(cores=cores, scene=scene):
                    candidates = generate_idblock_candidates(self.graph, cores, scene)
                    self.assertTrue(candidates)
                    for _, plan in candidates:
                        self.assertEqual(
                            derive_multicore_plan(self.graph, plan)["num_cores"],
                            cores)

    def test_idblock_pilot_improves_case_019_scene_a(self):
        plans = dict(generate_idblock_candidates(self.graph, 4, "A"))
        result = official_evaluate(self.graph, plans["idblock_32"],
                                   "A", self.config)
        self.assertLess(result["makespan"], 60000)


class RunnerContractTests(unittest.TestCase):
    def test_new_runs_are_distinct_and_resume_is_explicit(self):
        old_root = run_all.ROOT
        try:
            with tempfile.TemporaryDirectory() as temporary:
                run_all.ROOT = Path(temporary)
                args = SimpleNamespace(resume=None, quick=False, workers=1,
                                       search_hours=0, timeout_minutes=1)
                first, _ = run_all._create_or_resume(args, ["case_019"], [2])
                second, _ = run_all._create_or_resume(args, ["case_019"], [2])
                self.assertNotEqual(first, second)
                args.resume = first
                resumed, _ = run_all._create_or_resume(args, ["case_019"], [2])
                self.assertEqual(resumed, first.resolve())
        finally:
            run_all.ROOT = old_root

    def test_resume_rejects_configuration_mismatch(self):
        old_root = run_all.ROOT
        try:
            with tempfile.TemporaryDirectory() as temporary:
                run_all.ROOT = Path(temporary)
                args = SimpleNamespace(resume=None, quick=False, workers=1,
                                       search_hours=0, timeout_minutes=1)
                first, _ = run_all._create_or_resume(args, ["case_019"], [2])
                args.resume = first
                with self.assertRaises(ValueError):
                    run_all._create_or_resume(args, ["case_035"], [2])
        finally:
            run_all.ROOT = old_root


if __name__ == "__main__":
    unittest.main()
