import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run import scene_selection
from refine import input_footprints, advanced_neighbors, reorder
from official import DATA, ROOT, read_json, settings, canonical_hash, validate_candidate_structure, evaluate
import astra


class VersionEightTests(unittest.TestCase):
    def test_machine_splits_cover_every_required_job(self):
        cases = [f'case_{n:03d}' for n in range(1, 101)]
        first, second = scene_selection(cases, '1'), scene_selection(cases, '2')
        self.assertEqual([len(first[s]) for s in ('A', 'B', 'L2')], [100, 50, 50])
        self.assertEqual([len(second[s]) for s in ('A', 'B', 'L2')], [0, 100, 50])
        self.assertFalse(set(first['L2']) & set(second['L2']))
        self.assertEqual(set(first['L2']) | set(second['L2']), set(cases))
        self.assertEqual(first['L2'][0], 'case_051')
        self.assertEqual(second['L2'][-1], 'case_050')
        self.assertEqual(scene_selection(cases[:1], scenes=['L2'])['B'], cases[:1])

    def test_footprints_trace_copy_input_without_inventing_intermediate_hits(self):
        graph = {'tensors': [{'id': 1, 'pos': 'DDR', 'size': 128},
                             {'id': 2, 'pos': 'UB', 'size': 128},
                             {'id': 3, 'pos': 'UB', 'size': 128}],
                 'ops': [{'id': 10, 'op': 'COPY_IN'}, {'id': 11, 'op': 'ADD'},
                         {'id': 12, 'op': 'ADD'}, {'id': 13, 'op': 'ADD'}],
                 'edges': [{'source': a, 'target': b} for a, b in
                           [(1, 10), (10, 2), (2, 11), (2, 12), (11, 3), (3, 13)]]}
        footprints, _ = input_footprints(graph, {'11': 0, '12': 1, '13': 2})
        self.assertEqual(footprints[0], {1})
        self.assertEqual(footprints[1], {1})
        self.assertFalse(footprints[2])

    def test_advanced_moves_preserve_input_plan_and_exact_coverage(self):
        graph = read_json(DATA / 'case_001.json')
        plan = read_json(ROOT / 'reference/problem_1/plans/case_001_5core.json')
        before = canonical_hash(plan)
        labels = []
        for scene in ('A', 'B', 'L2'):
            candidates = list(advanced_neighbors(graph, plan, scene, settings(), {}))
            self.assertTrue(candidates)
            for label, candidate in candidates:
                labels.append(label)
                self.assertEqual(set(candidate['node_to_subgraph']), set(plan['node_to_subgraph']))
                self.assertEqual(len(candidate['core_schedules']), 5)
                self.assertEqual(set(candidate['node_to_subgraph'].values()),
                                 {s for row in candidate['core_schedules'] for s in row})
        self.assertTrue(any(l.startswith('worksplit') for l in labels))
        self.assertTrue(any(l.startswith('batchmerge') for l in labels))
        self.assertEqual(canonical_hash(plan), before)
        from solver import idblock_candidates
        graph = read_json(DATA / 'case_019.json')
        plan = next(idblock_candidates(graph, 5, (4,)))[1]
        labels = [label for label, _ in advanced_neighbors(graph, plan, 'B', settings(), {})]
        self.assertTrue(any(l.startswith('boundary') for l in labels))

    def test_cache_reorder_is_topological_and_officially_evaluable(self):
        graph = read_json(DATA / 'case_001.json')
        plan = read_json(ROOT / 'reference/problem_1/plans/case_001_5core.json')
        config = settings()
        result = reorder(graph, plan, 'cache', config)
        validate_candidate_structure(graph, result, 'L2', config)
        self.assertGreater(evaluate(graph, result, 'L2', config)['makespan'], 0)

    def test_seed_incumbent_cannot_be_replaced_by_worse_candidates(self):
        seed = {'node_to_subgraph': {'1': 0}, 'core_schedules': [[0], []]}
        other = {'node_to_subgraph': {'1': 1}, 'core_schedules': [[1], []]}
        def attempt(graph, plan, scene, config, label, evaluations, errors):
            metrics = {'makespan': 10 if plan == seed else 20, 'added_copy_bytes': 0}
            evaluations.append({'label': label, **metrics})
            return metrics
        with patch.object(astra, 'validate_candidate_structure'), \
             patch.object(astra, 'attempt', side_effect=attempt), \
             patch.object(astra, 'initial_candidates', return_value=iter([('worse', other)])):
            best, _, _, _, counts = astra.search({'ops': []}, 2, 'A', {}, 2, [('seed', seed)])
        self.assertEqual(best[2], seed)
        self.assertEqual(counts['official_evaluations'], 2)


if __name__ == '__main__':
    unittest.main()
