"""Check legal contraction and official scene-A improvement on a barrier graph."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from affinity_a import _acyclic_groups, affinity_candidates
from official import DATA, evaluate, read_json, settings
from target_a import bounded_attempt, one_task_plan


class AffinityTests(unittest.TestCase):
    def test_deadline_stops_official_evaluation(self):
        graph = read_json(DATA / "case_051.json")
        evaluations, errors = [], []
        started = time.perf_counter()
        result = bounded_attempt(graph, one_task_plan(graph, 5), settings(),
                                 "deadline_test", started - 1, evaluations, errors)
        self.assertIsNone(result)
        self.assertFalse(evaluations)
        self.assertIn("TimeoutError", errors[0]["error"])
        self.assertLess(time.perf_counter() - started, 5)

    def test_quotient_cycle_is_contracted(self):
        groups = _acyclic_groups([1, 2, 3],
                                  {1: {}, 2: {1: (1, False)}, 3: {2: (1, False)}},
                                  {1: 1, 2: 2, 3: 1})
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]), {1, 2, 3})

    def test_barrier_branches_pass_official_evaluation(self):
        graph = read_json(DATA / "case_051.json")
        config = settings()
        _, plan = next(affinity_candidates(graph, 5, config))
        metrics = evaluate(graph, plan, "A", config)
        self.assertEqual(sum(bool(row) for row in plan["core_schedules"]), 5)
        self.assertLess(metrics["makespan"], 607628 / 2)


if __name__ == "__main__":
    unittest.main()
