"""Pure manifest builder for NEW cache-bound, demand-driven PR reader pools.

Does not call Kubernetes, create resources or change original pools. Protected
orchestration must validate the reviewed catalogue, live volume identities and
resources before using a returned manifest. All three nodes are supported; no
per-node/fleet runner-count ceiling is authored.
"""
import copy

import incremental_cache as c
import node_cache_fleet_control as bounded
import node_cache_private_work as private_work

COMMON_ROUTE='cs-node-cache-7c14g'


def pool(policy,binding,original,storage_class,work_size_gib=private_work.WORKSPACE_GIB):
    env=policy['environment'];node=policy['node']
    c.require(env in ('mty','jed','ruh','mnl2') and node in ('mo-standard-0','mo-standard-1','mo-standard-2'),
              'unapproved demand-driven node')
    c.require(binding['environment']==env and binding['node']==node and
              binding['node_uid']==policy['node_uids'][node], 'demand-driven binding differs')
    # Reuse the already-tested pinned image, resource, auth-reference, readonly
    # mount and private workdir construction. Do not inherit any static bounds.
    result=bounded.pool(policy,binding,copy.deepcopy(original))
    number=node.rsplit('-',1)[1];name=f'cs-{env}-nc-n{number}-pr-v1'
    labels={'codex.matrixorigin.io/experiment':name,'app.kubernetes.io/name':name,
            'app.kubernetes.io/component':'autoscaling-runner-set','app.kubernetes.io/part-of':'gha-rs',
            'app.kubernetes.io/version':'0.14.1'}
    result['metadata'].update(name=name,labels=labels)
    spec=result['spec']
    spec['runnerScaleSetName']=name
    spec['runnerScaleSetLabels']=[COMMON_ROUTE,f'cs-{env}-node-cache-all']
    spec.pop('minRunners');spec.pop('maxRunners')
    spec['template']['metadata']['labels']=copy.deepcopy(labels)
    # No scale-set-name collision; common LABELS route across unique sets.
    c.require(name!=original['metadata']['name'] and
              COMMON_ROUTE not in original['spec'].get('runnerScaleSetLabels',[]), 'old route overlap')
    # Never emit a demand-driven shared PR route with writable work on the node
    # root. Storage admission is a per-Pod PVC request, not a runner-count cap.
    spec['template']['spec']=private_work.apply(spec['template']['spec'],storage_class,work_size_gib)
    return result
