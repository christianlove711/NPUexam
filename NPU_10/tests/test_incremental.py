import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from official import ROOT, DATA, read_json, atomic_json, canonical_hash, score, settings, derive_multicore_plan
from archive import load_pack, save_pack, paths
import run
import merge_runs
from branching import candidates


class IncrementalTests(unittest.TestCase):
    def fixture(self, directory, scene='A'):
        directory = Path(directory)
        m = {'options':{'small_budget':2,'large_budget':2,'small_seconds':30,'large_seconds':30,
                        'eval_seconds':1,'large_eval_seconds':1,'polish_strong':True},
             'sizes':{'case_064':906},
             'scene_cases':{s:['case_064'] if s==scene else [] for s in run.FOLDERS}}
        atomic_json(directory/'manifest.json',m)
        atomic_json(directory/'baseline/jobs/case_064.json',
                    read_json(ROOT/'seedpack/baseline/jobs/case_064.json'))
        return directory

    def test_timeout_kills_disposable_evaluator(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = load_pack(ROOT/'seedpack/case_064_5.json.gz')['A']['best']['plan']
            result = run.evaluate_timed(Path(tmp),'case_064',5,'A',plan,.001)
            self.assertFalse(result['ok'])
            self.assertIn('timeout',result['error'])

    def test_failed_candidates_preserve_incumbent_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = self.fixture(tmp)
            pack = load_pack(ROOT/'seedpack/case_064_5.json.gz')
            original = pack['A']['best']
            # Renumbering changes the hash but not placement or the pipe bound.
            proposals = []
            for offset in (1000000,2000000):
                p = original['plan']
                proposals.append((str(offset),{'node_to_subgraph':{n:s+offset for n,s in p['node_to_subgraph'].items()},
                                    'core_schedules':[[s+offset for s in row] for row in p['core_schedules']]}))
            def limited_candidates(*args):
                yield from proposals
                raise AssertionError('Generated another candidate after budget exhaustion')
            with patch.object(run,'candidates',side_effect=limited_candidates), \
                 patch.object(run,'local_candidates',return_value=iter([])), \
                 patch.object(run,'depth_candidates',return_value=iter([])), \
                 patch.object(run,'sibling_candidates',return_value=iter([])), \
                 patch.object(run,'partition_variants',return_value=iter([])), \
                 patch.object(run,'evaluate_timed',return_value={'ok':False,'error':'test timeout'}) as evaluate:
                run.worker(directory,'case_064',5)
            job = read_json(paths(directory,'case_064',5,'A')[0])
            self.assertEqual(job['selected_plan_hash'],original['plan_hash'])
            self.assertEqual(job['official_evaluations'],2)
            self.assertEqual(evaluate.call_count,2)
            self.assertEqual(job['evaluation_counts']['official_invalid'],2)
            self.assertEqual(len(job['errors']),2)

    def test_pending_pair_retries_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = self.fixture(tmp,'L2')
            m = read_json(directory/'manifest.json')
            m['options']['small_budget'] = 0
            atomic_json(directory/'manifest.json',m)
            pack = load_pack(ROOT/'seedpack/case_064_5.json.gz')
            b = pack['B']['best']
            pack['L2']['cache'].pop(b['plan_hash'],None)
            save_pack(directory/'cache/case_064_5.json.gz',pack)
            with patch.object(run,'evaluate_timed',return_value={'ok':False,'error':'test timeout'}):
                run.worker(directory,'case_064',5)
            jp,_ = paths(directory,'case_064',5,'L2')
            self.assertFalse(read_json(jp)['search_complete'])
            metric = copy.deepcopy(pack['L2']['best']['metrics'])
            with patch.object(run,'evaluate_timed',return_value={'ok':True,'metrics':metric}) as evaluate:
                run.worker(directory,'case_064',5)
            job = read_json(jp)
            self.assertEqual(evaluate.call_count,1)
            self.assertTrue(job['search_complete'])
            self.assertEqual(job['b_plan_hash'],b['plan_hash'])
            self.assertEqual(score(job['paired']['no_l2']),score(b['metrics']))
            self.assertEqual(job['official_evaluations'],2)
            self.assertEqual(job['budget'],2)

    def test_l2_only_does_not_search_its_b_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = self.fixture(tmp,'B')
            m = read_json(directory/'manifest.json')
            m['options']['search_scenes'] = ['L2']
            m['options']['polish_strong'] = True
            atomic_json(directory/'manifest.json',m)
            expected = load_pack(ROOT/'seedpack/case_064_5.json.gz')['B']['best']
            with patch.object(run,'evaluate_timed') as evaluate:
                run.worker(directory,'case_064',5)
            job = read_json(paths(directory,'case_064',5,'B')[0])
            self.assertEqual(evaluate.call_count,0)
            self.assertEqual(job['selected_plan_hash'],expected['plan_hash'])
            self.assertEqual(job['stop_reason'],'inherited_frozen')

    def test_bad_local_partition_does_not_abort_following_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = self.fixture(tmp)
            original = load_pack(ROOT/'seedpack/case_064_5.json.gz')['A']['best']['plan']
            def rename(offset):
                return {'node_to_subgraph':{n:s+offset for n,s in original['node_to_subgraph'].items()},
                        'core_schedules':[[s+offset for s in row] for row in original['core_schedules']]}
            rejected,accepted = rename(10000),rename(20000)
            import official
            actual_check = official.validate_candidate_structure
            def check(graph,plan,scene,*args):
                if plan==rejected:
                    raise run.MulticoreCutError('test cyclic quotient')
                return actual_check(graph,plan,scene,*args)
            with patch.object(run,'local_candidates',return_value=iter([('bad',rejected),('good',accepted)])), \
                 patch.object(run,'depth_candidates',return_value=iter([])), \
                 patch.object(run,'sibling_candidates',return_value=iter([])), \
                 patch.object(run,'candidates',return_value=iter([])), \
                 patch.object(run,'partition_variants',return_value=iter([])), \
                 patch.object(official,'validate_candidate_structure',side_effect=check), \
                 patch.object(run,'evaluate_timed',return_value={'ok':False,'error':'test timeout'}) as evaluate:
                run.worker(directory,'case_064',5)
            job = read_json(paths(directory,'case_064',5,'A')[0])
            self.assertEqual(job['preflight_rejections'],1)
            self.assertEqual(evaluate.call_count,1)

    def test_structural_candidates_cover_graph_and_have_acyclic_quotient(self):
        graph = read_json(DATA/'case_064.json')
        for scene in ('A','B','L2'):
            count = 0
            for label,plan in candidates(graph,5,scene,settings()):
                derive_multicore_plan(graph,plan)
                self.assertEqual(len(plan['core_schedules']),5)
                count += 1
            self.assertGreater(count,4)

    def test_merge_does_not_mix_a_faster_b_with_another_l2_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in (1,2):
                source = root/str(index)
                manifest = {'algorithm':'NPU_9','identity':{},'source_hashes':{},'seed_manifest_hash':'same',
                            'cases':['case_064'],'cores':[5],'sizes':{'case_064':906},
                            'scene_cases':{'A':[],'B':['case_064'],'L2':['case_064'] if index==1 else []}}
                atomic_json(source/'manifest.json',manifest)
                atomic_json(source/'baseline/jobs/case_064.json',{'makespan':1000,'status':'ok'})
                for scene in ('B','L2') if index==1 else ('B',):
                    jp,pp = paths(source,'case_064',5,scene)
                    atomic_json(jp,{'selected':{'makespan':100 if index==1 else 90,'added_copy_bytes':0}})
                    atomic_json(pp,{'owner':index})
            with patch.object(merge_runs,'validate',return_value={'status':'ok'}), \
                 patch.object(merge_runs,'summarize',return_value=([],{})):
                merge_runs.merge([root/'1',root/'2'],root/'merged')
            self.assertEqual(read_json(paths(root/'merged','case_064',5,'B')[1])['owner'],1)
            self.assertEqual(len(read_json(root/'merged/reports/merge_choices.json')),1)

if __name__ == '__main__':
    unittest.main()
