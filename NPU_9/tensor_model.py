"""Tensor-level B/L2 costs and separate L1/UB lifetime proxies.

The official builder copies once per input consumer core and once per actual
producer/consumer core pair. An op-edge sum would double count fan-out users.
These helpers do not estimate spill bytes or promise Cache hits.
"""
from collections import defaultdict
import heapq
from official import derive_multicore_plan


class TensorModel:
    def __init__(self, graph):
        self.ops = {o['id']: o for o in graph['ops']}
        self.eligible = {n for n, o in self.ops.items() if o['op'] not in {'COPY_IN', 'COPY_OUT'}}
        producers, consumers = defaultdict(set), defaultdict(set)
        self.direct = []
        for e in graph['edges']:
            a, b = e['source'], e['target']
            if a in self.ops and b in self.ops:
                self.direct.append((a, b, int(e.get('data_size', 0))))
            elif a in self.ops:
                producers[b].add(a)
            elif b in self.ops:
                consumers[a].add(b)
        self.tensors = {}
        for t in graph['tensors']:
            tid = t['id']
            ps, cs = producers[tid] & self.eligible, consumers[tid] & self.eligible
            if not ps and not cs:
                continue
            output = any(self.ops[n]['op'] == 'COPY_OUT' for n in consumers[tid]) or not cs
            self.tensors[tid] = {**t, 'producers': ps, 'consumers': cs, 'output': output}

    @staticmethod
    def owners(plan):
        owner = {s:c for c,row in enumerate(plan['core_schedules']) for s in row}
        return {int(n):owner[s] for n,s in plan['node_to_subgraph'].items()}

    def partition_bytes(self, plan):
        """Exact pre-spill scheduled bytes for the official B/L2 builder."""
        owner = self.owners(plan)
        total = 0
        for t in self.tensors.values():
            ps = {owner[n] for n in t['producers']}
            cs = {owner[n] for n in t['consumers']}
            reads = len(cs) if not ps else 0
            writes = len(ps) if t['output'] else 0
            pairs = sum(a != b for a in ps for b in cs)
            total += t['size'] * (reads + writes + 2*pairs)
        total += sum(2*size for a,b,size in self.direct
                     if a in owner and b in owner and owner[a] != owner[b])
        return total

    def read_footprints(self, plan):
        """Keys match COPY_IN's local tensor ID (not its synthetic DDR ID)."""
        mapping = {int(n):s for n,s in plan['node_to_subgraph'].items()}
        owner = self.owners(plan)
        position = {s:i for row in plan['core_schedules'] for i,s in enumerate(row)}
        footprints = defaultdict(set)
        for tid,t in self.tensors.items():
            ps = {owner[n] for n in t['producers']}
            cs = {owner[n] for n in t['consumers']}
            for core in cs:
                if not ps or any(p != core for p in ps):
                    group = min((mapping[n] for n in t['consumers'] if owner[n] == core),
                                key=lambda s:position[s])
                    footprints[group].add(tid)
        return footprints, {tid:t['size'] for tid,t in self.tensors.items()}


def memory_order(graph, plan, config):
    """Schedule ready groups to release live local tensors; capacities separate."""
    view = derive_multicore_plan(graph, plan)
    model = TensorModel(graph)
    owner = view['core_by_subgraph']
    mapping = view['mapping']
    uses, creates = defaultdict(set), defaultdict(set)
    remaining = defaultdict(set)
    for tid,t in model.tensors.items():
        for node in t['consumers']:
            sg=mapping[node]; uses[sg].add(tid); remaining[owner[sg],tid].add(sg)
        for node in t['producers']:
            creates[mapping[node]].add(tid)
    live = [set() for _ in plan['core_schedules']]
    occupied = [defaultdict(int) for _ in live]
    degree = {s:len(p) for s,p in view['subgraph_preds'].items()}
    ready = [s for s,d in degree.items() if not d]
    heapq.heapify(ready)
    rows = [[] for _ in live]
    def pos(tid):
        return model.tensors[tid]['pos'] if model.tensors[tid]['pos'] != 'DDR' else 'UB'
    def key(s):
        c=owner[s]; birth=(uses[s] | creates[s])-live[c]
        released={t for t in live[c]|birth if remaining[c,t] <= {s}}
        extra=defaultdict(int); freed=defaultdict(int)
        for t in birth: extra[pos(t)]+=model.tensors[t]['size']
        for t in released: freed[pos(t)]+=model.tensors[t]['size']
        peak=max((occupied[c][p]+extra[p])/config['capacity'][p] for p in ('L1','UB'))
        after=max((occupied[c][p]+extra[p]-freed[p])/config['capacity'][p] for p in ('L1','UB'))
        return (max(0,peak-1), after, -sum(freed.values()), s)
    while ready:
        candidates=[heapq.heappop(ready) for _ in range(min(64,len(ready)))]
        sg=min(candidates,key=key)
        for s in candidates:
            if s!=sg: heapq.heappush(ready,s)
        c=owner[sg]; rows[c].append(sg)
        for tid in (uses[sg]|creates[sg])-live[c]:
            live[c].add(tid);occupied[c][pos(tid)]+=model.tensors[tid]['size']
        for tid in uses[sg]: remaining[c,tid].discard(sg)
        for tid in list(live[c]):
            if not remaining[c,tid]:
                live[c].remove(tid);occupied[c][pos(tid)]-=model.tensors[tid]['size']
        for dst in view['subgraph_succs'][sg]:
            degree[dst]-=1
            if degree[dst]==0:heapq.heappush(ready,dst)
    return {'node_to_subgraph':dict(plan['node_to_subgraph']),'core_schedules':rows}


def locality_neighbors(graph, plan):
    """Co-locate a tensor's groups in one step, ranked by true boundary bytes."""
    model = TensorModel(graph)
    view = derive_multicore_plan(graph, plan)
    mapping, owners = view['mapping'], view['core_by_subgraph']
    tensors=[]
    for tid,t in model.tensors.items():
        groups={mapping[n] for n in t['producers']|t['consumers']}
        cores={owners[s] for s in groups}
        if len(cores)>1 and len(groups)<=8:
            tensors.append((-t['size']*(len(cores)-1),tid,groups))
    # One global topological order provides safe same-core insertion order.
    degree={s:len(p) for s,p in view['subgraph_preds'].items()}
    ready=[s for s,d in degree.items() if not d];heapq.heapify(ready);order=[]
    while ready:
        s=heapq.heappop(ready);order.append(s)
        for d in view['subgraph_succs'][s]:
            degree[d]-=1
            if degree[d]==0:heapq.heappush(ready,d)
    baseline=model.partition_bytes(plan)
    work=defaultdict(lambda:defaultdict(int))
    for n,s in mapping.items():work[s][model.ops[n]['pipe']]+=model.ops[n]['cycles']
    candidates=[]
    for _,tid,groups in sorted(tensors)[:8]:
        for target in sorted({owners[s] for s in groups}):
            rows=[[] for _ in plan['core_schedules']]
            loads=[defaultdict(int) for _ in rows]
            for s in order:
                c=target if s in groups else owners[s]
                rows[c].append(s)
                for pipe,value in work[s].items():loads[c][pipe]+=value
            candidate={'node_to_subgraph':dict(plan['node_to_subgraph']),'core_schedules':rows}
            traffic=model.partition_bytes(candidate)
            load=max((max(p.values(),default=0) for p in loads),default=0)
            # Bandwidth work and compute loads are only candidate priorities.
            candidates.append((traffic/60+load, traffic-baseline, tid, target,candidate))
    for _,_,tid,target,candidate in sorted(candidates,key=lambda x:x[:4])[:8]:
        yield f'tensorlocal_{tid}_{target}',candidate
