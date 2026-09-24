"""Focused checks for the targeted problem-one candidate guard."""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from graph import graph_view  # noqa: E402
from official import DATA, read_json, settings  # noqa: E402
from target_a import coarse_candidates, legal_task_order, one_task_plan  # noqa: E402
from run import job_paths, optimize, singlecore  # noqa: E402


class TargetedCandidateTests(unittest.TestCase):
    def test_dependency_guard_and_coarse_cut(self):
        graph = read_json(DATA / "case_019.json")
        dependencies = graph_view(graph)[2]
        self.assertTrue(legal_task_order(
            graph, one_task_plan(graph, 5), dependencies))
        _, candidate = next(coarse_candidates(graph, 5, settings()))
        self.assertTrue(legal_task_order(graph, candidate, dependencies))
        reversed_order = copy.deepcopy(candidate)
        reversed_order["core_schedules"][0].reverse()
        self.assertFalse(legal_task_order(
            graph, reversed_order, dependencies))

    def test_main_search_recovers_independent_components(self):
        with tempfile.TemporaryDirectory(prefix="npu5_components_") as directory:
            run = Path(directory)
            singlecore(run, "case_022", scene_a_only=True)
            optimize(run, "case_022", 5, 4, 4, scenes=("A",))
            job = read_json(job_paths(run, "case_022", 5, "A")[0])
            self.assertEqual(job["selected_source"], "components_total")
            self.assertEqual(job["selected"]["makespan"], 13013)


if __name__ == "__main__":
    unittest.main()
