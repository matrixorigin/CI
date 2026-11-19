"""Pure ALL-node demand-fleet proposal, never an apply or capacity acceptance.

Protected orchestration still authenticates producers/receipts and fresh live
identity/resources/activity before creating only these new resources.
"""
import copy

import incremental_cache as c
import node_cache_demand_fleet as demand
import node_compile_cache as cache
from node_cache_private_work import WORKSPACE_GIB

ENVIRONMENTS=('mty','jed','ruh','mnl2')
NODES=tuple(f'mo-standard-{i}' for i in range(3))


def plan(catalog,policy,original,storage_class,work_size_gib=WORKSPACE_GIB):
    c.require(catalog['schema']==2,'catalog schema differs')
    rows=catalog['nodes'];wanted={(e,n) for e in ENVIRONMENTS for n in NODES}
    c.require(len(rows)==12 and {(n['environment'],n['node']) for n in rows}==wanted,
              'all twelve independently reviewed node bindings are required')
    for key in ('pvc_uid','pv_uid'):
        c.require(len({n[key] for n in rows})==12,'a cache volume cannot be shared across nodes/environments')
    env=policy['environment'];c.require(env in ENVIRONMENTS,'unapproved plan environment')
    result=[]
    for node in NODES:
        binding=next(n for n in rows if n['environment']==env and n['node']==node)
        c.require(binding['node_uid']==policy['node_uids'][node],'reviewed/live node identity differs')
        identity={k:binding[k] for k in ('node_uid','pvc_uid','pv_uid')}
        entries=[e for e in catalog['entries'] if e['runtime']['environment']==env and identity in e.get('bindings',[])]
        c.require(entries and all(e['index_sha256'] for e in entries),'reviewed compilation bindings missing')
        local={**copy.deepcopy(policy),'node':node,'name':f'cs-{env}-incremental-n{node[-1]}-v1'}
        result.append(demand.pool(local,binding,original,storage_class,work_size_gib))
    c.require(len({x['metadata']['name'] for x in result})==3,'node pool identities collide')
    return result


def staged_plan(catalog,policy,original,storage_class,ready_nodes,observed_cpus,work_size_gib=WORKSPACE_GIB):
    """Explicit partial rollout; unready nodes remain targets, never block peers.

    This is only a proposal. The protected caller must authenticate the catalog
    and fresh CPU census, then recheck live resources before each new pool. A
    count of prepared nodes is NOT a min/max runner setting or fleet capacity.
    """
    c.require(isinstance(ready_nodes,(list,tuple)) and ready_nodes and
        len(set(ready_nodes))==len(ready_nodes) and set(ready_nodes)<=set(NODES),
        'explicit unique ready-node rollout scope required')
    c.require(set(observed_cpus)==set(NODES),'complete observed CPU inventory required')
    resources=plan(catalog,policy,original,storage_class,work_size_gib)
    env=policy['environment']
    for node in ready_nodes:
        binding=next(b for b in catalog['nodes'] if (b['environment'],b['node'])==(env,node))
        entries=cache.catalog_for_node(catalog,binding)['entries']
        exact=[e for e in entries if e['runtime']['cpu']==observed_cpus[node]]
        c.require(exact and any(e.get('native_manifests') for e in exact),
            'ready node lacks published exact-CPU compilation/native generation')
    return {'environment':env,'target_nodes':list(NODES),'ready_nodes':list(ready_nodes),
        'pending_nodes':[n for n in NODES if n not in ready_nodes],
        'resources':[r for r in resources if r['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname'] in ready_nodes],
        'runner_count_cap':None,'cluster_writes':0,'live_capacity_accepted':False}
