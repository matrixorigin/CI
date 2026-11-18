"""Pure ALL-node demand-fleet proposal, never an apply or capacity acceptance.

Protected orchestration still authenticates producers/receipts and fresh live
identity/resources/activity before creating only these new resources.
"""
import copy

import incremental_cache as c
import node_cache_demand_fleet as demand

ENVIRONMENTS=('mty','jed','ruh','mnl2')
NODES=tuple(f'mo-standard-{i}' for i in range(3))


def plan(catalog,policy,original,storage_class,work_size_gib=128):
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
