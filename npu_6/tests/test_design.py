from __future__ import annotations

import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import astra
from solver import idblock_candidates, refinement_sizes
from official import DATA, read_json, validate_candidate_structure


class V6DesignTests(unittest.TestCase):
    @staticmethod
    def metrics(makespan):
        return {"makespan": makespan, "added_copy_bytes": 0,
                "diagnostics": {"core_end": [makespan, 0],
                                "pipe_busy": [{}, {}], "late_subgraphs": []}}

    def test_fourth_version_refinement_table(self):
        expected = {
            4: (1, 2, 8), 16: (8, 24, 32), 48: (24, 32, 96),
            192: (96, 128, 384), 768: (384, 512, 1536),
        }
        for coarse, fine in expected.items():
            self.assertEqual(refinement_sizes(coarse), fine)

    def test_official_task_dag_check_is_preflighted_for_scene_a(self):
        graph = read_json(DATA / "case_001.json")
        plan = next(idblock_candidates(graph, 2, (4,)))[1]
        with patch("official.validate_task_order", wraps=__import__("official").validate_task_order) as check:
            view = validate_candidate_structure(graph, plan, "A")
            self.assertIn("core_orders", view)
            check.assert_called_once()
            check.reset_mock()
            self.assertIsNotNone(validate_candidate_structure(graph, plan, "B"))
            check.assert_not_called()

    def test_budget_counts_only_actual_official_calls(self):
        graph = read_json(DATA / "case_001.json")
        plan = next(idblock_candidates(graph, 2, (4,)))[1]

        def fake_attempt(_graph, _plan, _scene, _config, label, evaluations, _errors):
            result = self.metrics(100)
            evaluations.append({"label": label, **result})
            return result

        with patch.object(astra, "attempt", side_effect=fake_attempt):
            best, leaders, evaluations, errors, counts = astra.search(
                graph, 2, "A", {}, 1, extras=[("verified_seed", plan)])
        self.assertEqual(best[0], (100, 0))
        self.assertEqual(counts["official_evaluations"], 1)
        self.assertEqual(counts["official_valid"], 1)
        self.assertEqual(counts["official_invalid"], 0)
        self.assertEqual(counts["static_rejected"], 0)

    def test_large_graph_refines_only_the_best_coarse_grain(self):
        graph = {"ops": [{"op": "MATMUL"} for _ in range(5001)]}
        calls = []

        def fake_idblocks(_graph, _cores, sizes):
            for size in sizes:
                yield f"fixed_{size}", {
                    "node_to_subgraph": {"0": int(size)},
                    "core_schedules": [[int(size)]],
                }

        def fake_attempt(_graph, plan, _scene, _config, label, evaluations, _errors):
            calls.append(label)
            grain = next(iter(plan["node_to_subgraph"].values()))
            if label.startswith("coarse_"):
                makespan = {4: 100, 16: 80, 48: 110, 192: 120, 768: 130}[grain]
            else:
                makespan = 70 + grain
            result = self.metrics(makespan)
            evaluations.append({"label": label, **result})
            return result

        with patch.object(astra, "idblock_candidates", side_effect=fake_idblocks), \
             patch.object(astra, "initial_candidates", return_value=iter(())), \
             patch.object(astra, "neighbors", return_value=iter(())), \
             patch.object(astra, "validate_candidate_structure", return_value={}), \
             patch.object(astra, "attempt", side_effect=fake_attempt):
            _best, _leaders, _evaluations, _errors, counts = astra.search(
                graph, 2, "A", {}, 8)
        self.assertEqual(counts["official_evaluations"], 8)
        self.assertEqual(calls[:5], [f"coarse_fixed_{n}" for n in (4, 16, 48, 192, 768)])
        self.assertEqual(calls[5:], [f"refine_fixed_{n}" for n in (8, 24, 32)])


if __name__ == "__main__":
    unittest.main()
