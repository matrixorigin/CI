"""All-node reader registration and initial-job admission, without count caps.

Registration budgets listeners only. A separate initial-job gate budgets real
private work claims and 7c/14Gi jobs; unfit nodes wait without being deleted or
silently removed from the target fleet. No cluster writes in this module.
"""
import copy
import datetime as dt

import incremental_cache as c
import node_compile_cache as cache
import node_cache_demand_plan as plan
from node_cache_private_work import WORKSPACE_GIB
import node_cache_cpu_producer_fleet as capacity
import node_cache_workspace_probe_control as q

CPU_PLAN_SHA256='470c8418604b927709ff36db9001c8cac204845ae774c9889ca3bedd49cee334'
CPU_AUTHORITIES={
    'jed':(44308679217,'34f09933cd2ab9bea0d63a395fc49a5446ec80b2',14429903368,
        'sha256:30cfba03355fb694a339f70835bf42076018fb3b5a00d9d5b11275717f59154f'),
    'mnl2':(44307675272,'3ec65381e692dee7acb7c5e39c78280ce906c5fb',14429552845,
        'sha256:e20cc692c1614d11f99f9bd62ac360cb4d1a3ace04672dc8def73df6bc293976'),
    'ruh':(44307680041,'3ec65381e692dee7acb7c5e39c78280ce906c5fb',14430206827,
        'sha256:46a2036c9792eaf2ae349a1bdae2cb32e87324c341cfd16661918916854bdfbf')}


def cpu_inventory(reviewed,base,environment,api):
    c.require(cache.hash_value(reviewed)==CPU_PLAN_SHA256 and environment in CPU_AUTHORITIES,
        'independently reviewed full CPU census changed')
    runid,revision,artifactid,digest=CPU_AUTHORITIES[environment]
    run=api(f'repos/matrixorigin/cloudsigma-gitops/actions/runs/{runid}')
    c.require(run['id']==runid and run['head_sha']==revision and run['run_attempt']==1
        and run['status']=='completed' and run['conclusion']=='success' and run['event']=='workflow_dispatch'
        and run['head_branch']=='codex/ruh-pvc-readiness' and run['actor']['login']==run['triggering_actor']['login']=='Ariznawlll'
        and run['path']=='.github/workflows/cloudsigma-up.yaml'
        and run['display_title']==f'up cs-component / {environment} / node-cache-workspace-probe',
        'independent exact-CPU workspace proof run differs')
    artifact=api(f'repos/matrixorigin/cloudsigma-gitops/actions/artifacts/{artifactid}')
    c.require(artifact['id']==artifactid and artifact['digest']==digest and not artifact['expired']
        and artifact['workflow_run']['id']==runid and artifact['workflow_run']['head_sha']==revision,
        'independent CPU workspace proof transport differs')
    rows=[v for v in reviewed['observations'] if v['environment']==environment]
    c.require(len(rows)==3 and {v['node'] for v in rows}==set(plan.NODES),'CPU census node coverage differs')
    result={}
    for row in rows:
        bound=next(v for v in base['nodes'] if (v['environment'],v['node'])==(environment,row['node']))
        c.require(row['binding']==bound and row['evidence_source']['run_id']==runid
            and row['evidence_source']['artifact_digest']==digest and row['readonly'] is True
            and row['cpu_sha256']==cache.hash_value(row['cpu']),'CPU proof binding or actual profile differs')
        result[row['node']]=row['cpu']
    return result


def resources(catalog,policy,original,storage_class,nodes,observed_cpus):
    proposal=plan.staged_plan(catalog,policy,original,storage_class,nodes,observed_cpus)
    for resource in proposal['resources']:
        listener=resource['spec']['listenerTemplate']['spec']
        c.require(len(listener['containers'])==1 and listener['containers'][0]['name']=='listener'
            and not listener.get('initContainers'),'unreviewed reader listener template')
        listener['containers'][0]['resources']=copy.deepcopy(capacity.LISTENER_RESOURCES)
        listener['nodeSelector']={'kubernetes.io/hostname':'mo-standard-0','matrixone.cloud/profile':'cn.cloudsigma'}
        listener['tolerations']=copy.deepcopy(resource['spec']['template']['spec']['tolerations'])
        c.require(not {'minRunners','maxRunners'}&set(resource['spec']),'reader count cap authored')
    return proposal


def available_entries(catalog,binding,cpu,now=None):
    now=now or q.pub.now()
    entries=cache.catalog_for_node(catalog,binding)['entries']
    exact=[e for e in entries if e['runtime']['cpu']==cpu]
    available=[];expired=[]
    for entry in exact:
        deadline=dt.datetime.fromisoformat(entry['expires_at'].replace('Z','+00:00'))
        c.require(deadline.tzinfo is not None,'cache expiry lacks timezone')
        (available if deadline>now else expired).append(entry)
    c.require(available and any(e.get('native_manifests') for e in available),
        'no unexpired published exact-CPU native/Go generation; prepare missing generation')
    return available,expired


def authenticate(base,lock,evidence,environment,nodes,observed_cpus,api):
    # Preserve the exact independently copied base including expired records.
    # Expired generations are explicitly excluded, not renewed or authorized.
    c.require(environment in plan.ENVIRONMENTS and nodes and len(set(nodes))==len(nodes)
        and set(nodes)<=set(plan.NODES) and set(observed_cpus)==set(plan.NODES),'reader authority scope differs')
    c.require(evidence['schema']==1 and evidence['catalog_canonical_sha256']==cache.hash_value(base),
        'copied catalog/evidence changed')
    row=next(v for v in evidence['copies'] if v['environment']==environment)
    run=api(f'repos/matrixorigin/cloudsigma-gitops/actions/runs/{row["run_id"]}')
    c.require(run['id']==row['run_id'] and run['head_sha']==row['gitops_sha'] and run['conclusion']=='success'
        and run['status']=='completed' and run['event']=='workflow_dispatch' and run['run_attempt']==1
        and run['head_branch']=='codex/ruh-pvc-readiness' and run['path']=='.github/workflows/cloudsigma-up.yaml'
        and run['actor']['login']=='Ariznawlll' and run['triggering_actor']['login']=='Ariznawlll'
        and run['display_title']==f'up cs-component / {environment} / node-cache-replicate',
        'independently copied run authority differs')
    artifact=api(f'repos/matrixorigin/cloudsigma-gitops/actions/artifacts/{row["artifact_id"]}')
    c.require(artifact['digest']==row['artifact_digest'] and artifact['expired'] is False
        and artifact['workflow_run']['id']==row['run_id'] and artifact['workflow_run']['head_sha']==row['gitops_sha'],
        'raw copy receipt API differs')
    valid={};excluded={}
    for node in nodes:
        binding=next(v for v in base['nodes'] if (v['environment'],v['node'])==(environment,node))
        entries,expired=available_entries(base,binding,observed_cpus[node])
        for entry in entries:
            c.require(cache.reviewed_entries({'entries':[entry]},entry['runtime'],api)==[entry],
                'unexpired producer/artifact provenance differs')
        valid[node]=[e['index_sha256'] for e in entries]
        excluded[node]=[e['index_sha256'] for e in expired]
    q.base.tools.authorize(lock,api(f'repos/{lock["producer"]["repository"]}/actions/runs/{lock["producer"]["id"]}'),
        api(f'repos/{lock["producer"]["repository"]}/actions/artifacts/{lock["artifact"]["id"]}'))
    return {'copy_run':run['id'],'valid_exact_cpu_indexes':valid,'expired_indexes_not_renewed_or_used':excluded}


def admission(policy,resources,snapshots,owned=(),job_nodes=()):
    c.require(resources and 1<=len(resources)<=3 and len(set(job_nodes))==len(job_nodes),
        'reader registration scope differs')
    targets={r['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname'] for r in resources}
    c.require(len(targets)==len(resources) and targets<=set(plan.NODES) and set(job_nodes)<=targets,
        'initial-job nodes not in registered fleet')
    extras={n:[] for n in plan.NODES};specs=[]
    for resource in resources:
        spec=resource['spec'];pod=spec['template']['spec'];listener=spec['listenerTemplate']['spec']
        c.require(not {'minRunners','maxRunners'}&set(spec) and
            pod['containers'][0]['resources']=={'requests':{'cpu':'7','memory':'12Gi'},'limits':{'cpu':'7','memory':'14Gi'}}
            and listener['containers'][0]['resources']==capacity.LISTENER_RESOURCES
            and listener['nodeSelector']['kubernetes.io/hostname']=='mo-standard-0','reader contract changed')
        claim=pod['volumes'][0]['ephemeral']['volumeClaimTemplate']['spec']
        c.require(q.pub.quantity(claim['resources']['requests']['storage'])==WORKSPACE_GIB*c.GIB
            and pod['volumes'][1]['persistentVolumeClaim']['readOnly'] is True
            and pod['containers'][0]['volumeMounts'][1]['readOnly'] is True,'private/shared work boundary changed')
        extras['mo-standard-0'].append(listener);specs.append(listener)
        node=pod['nodeSelector']['kubernetes.io/hostname']
        if node in job_nodes:extras[node].append(pod);specs.append(pod)
    result={}
    for node in plan.NODES:
        snapshot=snapshots[node];q.admission(policy,snapshot,node,owned)
        alloc=next(v for v in snapshot['nodes']['items'] if v['metadata']['name']==node)['status']['allocatable']
        additional=[sum(capacity.effective_budget(v)[i] for v in extras[node]) for i in (0,1)]
        total=additional[:]
        for pod in snapshot['pods']['items']:
            if pod['metadata']['uid'] in owned or pod['spec'].get('nodeName')!=node or pod.get('status',{}).get('phase') in ('Failed','Succeeded'):continue
            amount=q.pub.pod_budget(pod);total=[a+b for a,b in zip(total,amount)]
        stats=snapshot['stats']['node']
        c.require(total[0]+2<=q.pub.quantity(alloc['cpu']) and total[1]+4*c.GIB<=q.pub.quantity(alloc['memory'])
            and stats['memory']['availableBytes']>=additional[1]+4*c.GIB
            and stats['fs']['availableBytes']>=60*c.GIB,'new reader/listener resources unavailable; wait')
        free=max((d['status'].get('freeCapacity',0) for d in snapshot['drives']['items']
            if d['metadata'].get('labels',{}).get('directpv.min.io/node')==node
            and d['metadata']['labels'].get('directpv.min.io/tier')=='ephemeral'
            and d['status'].get('status')=='Ready'),default=0)
        c.require(free>=(WORKSPACE_GIB*int(node in job_nodes)+20)*c.GIB,'private workspace capacity unavailable; wait')
        result[node]={'root_free_bytes':stats['fs']['availableBytes'],'memory_available_bytes':stats['memory']['availableBytes'],
            'physical_free_bytes':free,'combined_cpu_limits':total[0],'combined_memory_limits_bytes':total[1],
            'initial_private_claims':int(node in job_nodes)}
    required={'pods':len(specs),'count/pods':len(specs),'persistentvolumeclaims':len(job_nodes),
        'count/persistentvolumeclaims':len(job_nodes),'requests.storage':len(job_nodes)*WORKSPACE_GIB*c.GIB,
        'directpv-min-io-ephemeral.storageclass.storage.k8s.io/persistentvolumeclaims':len(job_nodes),
        'directpv-min-io-ephemeral.storageclass.storage.k8s.io/requests.storage':len(job_nodes)*WORKSPACE_GIB*c.GIB}
    for kind in ('requests','limits'):
        values=[]
        for spec in copy.deepcopy(specs):
            for container in spec['containers']+spec.get('initContainers',[]):
                container['resources']={kind:container.get('resources',{}).get(kind,{})}
            values.append(capacity.effective_budget(spec))
        for i,key in enumerate(('cpu','memory')):required[kind+'.'+key]=sum(v[i] for v in values)
    for quota in snapshots['mo-standard-0']['quotas']['items']:
        status=quota.get('status',{});hard=status.get('hard',quota.get('spec',{}).get('hard',{}));used=status.get('used',{})
        for key,needed in required.items():
            if key in hard:c.require(q.pub.quantity(used.get(key,0))+needed<=q.pub.quantity(hard[key]),'reader namespace quota unavailable; wait')
    return {'nodes':result,'registered_nodes':sorted(targets),'initial_job_nodes':list(job_nodes),
        'runner_count_cap':None,'actual_multi_pr_capacity_accepted':False,'cluster_writes':0}
