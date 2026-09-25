"""Small, incumbent-directed neighborhoods; never change the official config."""
from collections import defaultdict
from itertools import islice, zip_longest
import re
from graph import graph_view
from partition import _topological_orders, _schedule_blocks
from affinity_a import _bundle_siblings, _acyclic_groups
from refine import advanced_neighbors
from solver import neighbors


def local_candidates(graph, plan, scene, config, metrics):
    # Round-robin avoids spending the entire budget on only one move family.
    advanced = islice(advanced_neighbors(graph,plan,scene,config,metrics.get('diagnostics',{})),12)
    basic = islice((x for x in neighbors(graph,plan,scene,limit=36)
                    if not x[0].startswith('split_')),12)
    emitted = 0
    for pair in zip_longest(advanced,basic):
        for item in pair:
            if item is not None:
                yield 'local_'+item[0],item[1]
                emitted += 1
                if emitted >= 12:
                    return


def depth_candidates(graph, cores, scene, config, selected_label):
    ops,order,preds = graph_view(graph)
    _,rank,output = _topological_orders(ops,order,preds)
    depth = {}
    for n in order:
        depth[n] = 1 + max((depth[p] for p in preds[n]),default=-1)
    match = re.search(r'depth_(?:islands|shift)_(\d+)',selected_label)
    width = int(match.group(1)) if match else 16
    widths = list(dict.fromkeys((width,max(3,round(width*.75)),round(width*1.25))))
    for w in widths:
        for offset in (w//2,0):
            # Original zero-offset powers of two are already in the old cache.
            if offset==0 and w in (2,4,8,16,32,64):
                continue
            parent = {n:n for n in order}
            def find(n):
                while parent[n] != n:
                    parent[n] = parent[parent[n]]
                    n = parent[n]
                return n
            for n in order:
                for p in preds[n]:
                    if (depth[n]+offset)//w == (depth[p]+offset)//w:
                        parent[find(n)] = find(p)
            grouped = defaultdict(list)
            for n in order:
                grouped[find(n)].append(n)
            blocks = _bundle_siblings(list(grouped.values()),preds,ops,cores,max_nodes=max(2048,len(order)))
            mapping = {n:i for i,b in enumerate(blocks) for n in b}
            plan,_ = _schedule_blocks(blocks,mapping,ops,preds,rank,cores,scene,config,output)
            yield f'depth_shift_{w}_{offset}',plan


def sibling_candidates(graph, cores, scene, config):
    """Vary task granularity inside equivalent branches, not the hardware cores."""
    ops,order,preds = graph_view(graph)
    _,rank,output = _topological_orders(ops,order,preds)
    outgoing = defaultdict(int)
    for links in preds.values():
        for p in links:
            outgoing[p] += 1
    barriers = {n for n in order if max(len(preds[n]),outgoing[n])>=3}
    parent = {n:n for n in order}
    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    for n in order:
        if n not in barriers:
            for p in preds[n]:
                if p not in barriers:
                    parent[find(n)] = find(p)
    original = _acyclic_groups(order,preds,parent)
    for bins in (max(2,cores-1),cores+1,cores*2):
        blocks = _bundle_siblings(original,preds,ops,bins,max_nodes=max(2048,len(order)))
        mapping = {n:i for i,b in enumerate(blocks) for n in b}
        plan,_ = _schedule_blocks(blocks,mapping,ops,preds,rank,cores,scene,config,output)
        yield f'fork_rebundle_{bins}',plan
