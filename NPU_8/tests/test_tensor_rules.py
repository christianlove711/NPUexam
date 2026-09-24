import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from official import ROOT, DATA, read_json, settings, build_b_tasks, evaluate, canonical_hash
from solver import idblock_candidates
from tensor_model import TensorModel, memory_order, locality_neighbors


class TensorRulesTests(unittest.TestCase):
    def test_partition_bytes_match_official_builder(self):
        config=settings()
        for name in ('case_019','case_057'):
            graph=read_json(DATA/(name+'.json'))
            plan=read_json(ROOT/'reference/problem_2/plans'/(name+'_5core.json'))
            *_,traffic,view=build_b_tasks(graph,plan,config['bandwidth'],config['capacity'])
            self.assertEqual(TensorModel(graph).partition_bytes(plan),
                             traffic['scheduled_copy_bytes']-traffic['spill_added_copy_bytes'])

    def test_input_key_is_local_tensor_and_fanout_copy_is_per_core(self):
        graph={'ops':[{'id':1,'op':'COPY_IN'}, {'id':2,'op':'ADD'}, {'id':3,'op':'ADD'},
                      {'id':4,'op':'ADD'}],
               'tensors':[{'id':10,'pos':'DDR','size':64}, {'id':11,'pos':'UB','size':64},
                          {'id':12,'pos':'UB','size':128}],
               'edges':[{'source':a,'target':b} for a,b in
                        [(10,1),(1,11),(11,2),(2,12),(12,3),(12,4)]]}
        plan={'node_to_subgraph':{'2':0,'3':1,'4':2},'core_schedules':[[0],[1,2]]}
        model=TensorModel(graph)
        # Input: 64; one source->target transfer despite two consumers: 2*128.
        self.assertEqual(model.partition_bytes(plan),320)
        reads,sizes=model.read_footprints(plan)
        self.assertEqual(reads[0],{11})
        self.assertEqual(reads[1],{12})
        self.assertNotIn(10,reads[0])
        self.assertFalse(reads[2])
        merged={'node_to_subgraph':{'2':0,'3':1,'4':1},'core_schedules':[[0],[1]]}
        self.assertEqual(model.partition_bytes(merged),model.partition_bytes(plan))

    def test_new_orders_preserve_coverage_without_mutating_seed(self):
        graph=read_json(DATA/'case_019.json')
        plan=read_json(ROOT/'reference/problem_2/plans/case_019_5core.json')
        original=canonical_hash(plan)
        changed=memory_order(graph,plan,settings())
        self.assertEqual(changed['node_to_subgraph'],plan['node_to_subgraph'])
        for label,other in locality_neighbors(graph,plan):
            self.assertEqual(other['node_to_subgraph'],plan['node_to_subgraph'])
            flat=[s for row in other['core_schedules'] for s in row]
            self.assertEqual(len(flat),len(set(flat)))
            self.assertEqual(set(flat),set(plan['node_to_subgraph'].values()))
        self.assertEqual(canonical_hash(plan),original)

    def test_official_diagnostics_decompose_actual_traffic_and_misses(self):
        graph=read_json(DATA/'case_019.json')
        plan=read_json(ROOT/'reference/problem_2/plans/case_019_5core.json')
        metric=evaluate(graph,plan,'L2',settings())
        self.assertEqual(metric['added_copy_bytes'],metric['partition_added_copy_bytes']+metric['spill_added_copy_bytes'])
        self.assertEqual(sum(metric['diagnostics']['cache_miss_reasons'].values()),metric['cache_miss_bytes'])


if __name__=='__main__':unittest.main()
