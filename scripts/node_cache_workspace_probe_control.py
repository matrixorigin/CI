"""Journalled all-node PRIVATE-workspace/readonly-cache lifecycle probe.

Only creates new ConfigMap/Pods and UID-deletes its own Pods. Workspace PVCs
are created/collected by Kubernetes; cache volumes are never adopted or written.
"""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
import subprocess
import threading
import time
import traceback

import incremental_cache as c
import node_cache_demand_fleet as demand
import node_cache_publish_control as pub
import node_cache_replicate_control as base
import node_cache_demand_plan as planner
import node_cache_partial_recover_control as volume_checks
import node_cache_chunk_resume_control as sessions

# Auxiliary proof volume only, within the reviewed 80..256Gi work contract.
# Normal demand fleet defaults remain independent and require real UT sizing.
WORK_GIB=80
PROBE_RESOURCES={'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'1','memory':'512Mi'}}


def probe_pod(pool,name,config_map):
    c.require(pool['kind']=='AutoscalingRunnerSet' and pool['metadata']['name'].startswith('cs-')
        and demand.COMMON_ROUTE in pool['spec']['runnerScaleSetLabels'],'reviewed demand proposal required')
    spec=copy.deepcopy(pool['spec']['template']['spec'])
    c.require('ephemeral' in spec['volumes'][0] and spec['volumes'][1]['persistentVolumeClaim']['readOnly'] is True
        and spec['automountServiceAccountToken'] is False,'private work/cache boundaries missing')
    claim=spec['volumes'][0]['ephemeral']['volumeClaimTemplate']['spec']
    c.require(pub.quantity(claim['resources']['requests']['storage'])==WORK_GIB*c.GIB,'auxiliary workspace size differs')
    spec.pop('serviceAccountName',None)
    spec.update(restartPolicy='Never',activeDeadlineSeconds=3600)
    runner=spec['containers'][0]
    runner.update(command=['/bin/bash','-euc','sleep 3500'],resources=copy.deepcopy(PROBE_RESOURCES),
        securityContext={'runAsNonRoot':True,'allowPrivilegeEscalation':False,'privileged':False,
            'capabilities':{'drop':['ALL']}})
    runner['env'].append({'name':'CS_PROBE_UID','valueFrom':{'fieldRef':{'fieldPath':'metadata.uid'}}})
    spec['volumes'].append({'name':'probe-script','configMap':{'name':config_map}})
    runner['volumeMounts'].append({'name':'probe-script','mountPath':'/probe','readOnly':True})
    return {'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':'cicd',
        'labels':{'codex.matrixorigin.io/workspace-probe':config_map}},'spec':spec}


def checked_pod(expected,actual,uid):
    c.require(actual['metadata']['uid']==uid and not actual['metadata'].get('deletionTimestamp')
        and not actual['metadata'].get('ownerReferences') and base.checked_helper_spec(expected['spec'],actual['spec']),
        'probe Pod UID or template changed')
    spec=actual['spec']
    c.require(not any(spec.get(k,False) for k in ('hostNetwork','hostPID','hostIPC')),'probe host isolation differs')
    for container in spec['containers']+spec.get('initContainers',[]):
        security=container.get('securityContext',{})
        c.require(not security.get('privileged',False) and security.get('allowPrivilegeEscalation') is False
            and not security.get('capabilities',{}).get('add',[]),'probe gained container privileges')
    status=actual.get('status',{})
    c.require(status.get('phase') not in ('Failed','Succeeded') and not any(
        x.get('restartCount',0) for x in status.get('containerStatuses',[])+status.get('initContainerStatuses',[])),
        'probe exited or restarted')
    return status.get('phase')=='Running' and any(x['type']=='Ready' and x['status']=='True' for x in status.get('conditions',[]))


def quotas(snapshot,claims,pods):
    wanted={'persistentvolumeclaims':claims,'count/persistentvolumeclaims':claims,
        'requests.storage':claims*WORK_GIB*c.GIB,'pods':pods,'count/pods':pods,
        'requests.cpu':pods*.1,'requests.memory':pods*128*1024**2,
        # Conservatively include the non-root initialization limits as well.
        'limits.cpu':pods*2,'limits.memory':pods*c.GIB}
    wanted.update({'directpv-min-io-ephemeral.storageclass.storage.k8s.io/persistentvolumeclaims':claims,
        'directpv-min-io-ephemeral.storageclass.storage.k8s.io/requests.storage':claims*WORK_GIB*c.GIB})
    for quota in snapshot['quotas']['items']:
        status=quota.get('status',{});hard=status.get('hard',quota.get('spec',{}).get('hard',{}));used=status.get('used',{})
        for key,needed in wanted.items():
            if key in hard:
                c.require(pub.quantity(used.get(key,0))+needed<=pub.quantity(hard[key]),'private workspace quota unavailable; wait without changing quota')


def admission(policy,snapshot,node,owned=(),baseline=None,new_claims=0,new_pods=0):
    # The copy controller's default 64Gi *unallocated drive* requirement is
    # unrelated to existing Pod-owned workspaces. Keep the 20Gi drive reserve
    # here, then budget every NEW workspace below. Root still requires 60Gi.
    proof=base.snapshot_guard(policy,snapshot,node,owned,baseline,physical_reserve_gib=20)
    sc=snapshot['sc']
    c.require(sc['metadata']['name']=='directpv-min-io-ephemeral' and not sc['metadata'].get('deletionTimestamp')
        and sc['reclaimPolicy']=='Delete','private workspace class lifecycle differs')
    quotas(snapshot,new_claims,new_pods)
    free=max((d['status'].get('freeCapacity',0) for d in snapshot['drives']['items']
        if d['metadata'].get('labels',{}).get('directpv.min.io/node')==node
        and d['metadata']['labels'].get('directpv.min.io/tier')=='ephemeral'
        and d['status'].get('status')=='Ready'),default=0)
    c.require(free>=new_claims*WORK_GIB*c.GIB+20*c.GIB,'private workspace physical reservation unavailable')
    allocated=next(n for n in snapshot['nodes']['items'] if n['metadata']['name']==node)['status']['allocatable']
    # At most TWO small probes coexist, not two full-size UT runners. Original
    # UT resources/timeouts and demand-driven runner counts remain unchanged.
    cpu,memory=4.,2*c.GIB
    for pod in snapshot['pods']['items']:
        if pod['metadata']['uid'] in owned or pod['spec'].get('nodeName')!=node or pod.get('status',{}).get('phase') in ('Failed','Succeeded'):continue
        a,b=pub.pod_budget(pod);cpu+=a;memory+=b
    c.require(cpu+2<=pub.quantity(allocated['cpu']) and memory+4*c.GIB<=pub.quantity(allocated['memory']),
        'two auxiliary probes do not fit declared capacity reserves')
    c.require(snapshot['stats']['node']['memory']['availableBytes']>=4*c.GIB+new_pods*c.GIB,
        'auxiliary probe actual memory reserve')
    return {**proof,'storage_class_uid':sc['metadata']['uid'],'planned_private_claims':new_claims,
        'per_claim_bytes':WORK_GIB*c.GIB,'physical_free_bytes':free,'auxiliary_probe_only':True}


def auxiliary_slots(snapshot,node):
    """Capacity-derived AUXILIARY proof slots, never a runner fleet limit."""
    free=max((d['status'].get('freeCapacity',0) for d in snapshot['drives']['items']
        if d['metadata'].get('labels',{}).get('directpv.min.io/node')==node
        and d['metadata']['labels'].get('directpv.min.io/tier')=='ephemeral'
        and d['status'].get('status')=='Ready'),default=0)
    slots=min(2,int((free-20*c.GIB)//(WORK_GIB*c.GIB)))
    c.require(slots>=1,'no private workspace fits reserve; wait without creating resources')
    return slots


def private_volume(pod,pvc,pv,node,cache_binding):
    name=pod['metadata']['name']+'-work';uid=pod['metadata']['uid']
    c.require(pod['spec']['nodeName']==node and pvc['metadata']['name']==name and pvc['metadata']['namespace']=='cicd'
        and not pvc['metadata'].get('deletionTimestamp') and pvc['metadata']['uid']!=cache_binding['pvc_uid'],
        'private workspace claim identity differs')
    owners=pvc['metadata'].get('ownerReferences',[])
    c.require(len(owners)==1 and all(owners[0].get(k)==v for k,v in
        {'apiVersion':'v1','kind':'Pod','name':pod['metadata']['name'],'uid':uid,'controller':True}.items()),
        'workspace claim is not exclusively owned by this probe Pod')
    c.require(pvc['status']['phase']==pv['status']['phase']=='Bound'
        and pvc['spec']['volumeName']==pv['metadata']['name'] and not pv['metadata'].get('deletionTimestamp')
        and pv['metadata']['uid']!=cache_binding['pv_uid'] and not pv['metadata'].get('ownerReferences')
        and all(pv['spec']['claimRef'].get(k)==v for k,v in
            {'name':name,'namespace':'cicd','uid':pvc['metadata']['uid']}.items()),'private workspace PV binding differs')
    for obj in (pvc,pv):
        c.require(obj['spec']['storageClassName']=='directpv-min-io-ephemeral'
            and obj['spec']['accessModes']==['ReadWriteOnce'] and obj['spec'].get('volumeMode','Filesystem')=='Filesystem',
            'private workspace storage topology differs')
    c.require(pub.quantity(pvc['spec']['resources']['requests']['storage'])==WORK_GIB*c.GIB
        and pub.quantity(pvc['status']['capacity']['storage'])==WORK_GIB*c.GIB
        and pub.quantity(pv['spec']['capacity']['storage'])==WORK_GIB*c.GIB
        and pv['spec'].get('persistentVolumeReclaimPolicy')=='Delete'
        and pv['spec']['csi']['driver']=='directpv-min-io','private workspace size or driver differs')
    terms=pv['spec']['nodeAffinity']['required']['nodeSelectorTerms']
    c.require(len(terms)==1 and not terms[0].get('matchFields') and any(
        x=={'key':'directpv.min.io/node','operator':'In','values':[node]} for x in terms[0]['matchExpressions']),
        'private workspace is not on the cache node')
    return {'pod_uid':uid,'pvc_name':name,'pvc_uid':pvc['metadata']['uid'],'pv_uid':pv['metadata']['uid'],'bytes':WORK_GIB*c.GIB}


def authenticate(catalog,lock,evidence,environment,api):
    c.require(evidence['schema']==1 and evidence['catalog_canonical_sha256']==base.replication.sha(c.canonical(catalog)),
        'all-node catalog evidence digest differs')
    rows=[r for r in evidence['copies'] if r['environment']==environment]
    c.require(len(rows)==1,'environment copy authority missing')
    row=rows[0];run=api(f'repos/matrixorigin/cloudsigma-gitops/actions/runs/{row["run_id"]}')
    c.require(run['id']==row['run_id'] and run['head_sha']==row['gitops_sha'] and run['conclusion']=='success'
        and run['status']=='completed' and run['event']=='workflow_dispatch' and run['run_attempt']==1
        and run['head_branch']=='codex/ruh-pvc-readiness' and run['path']=='.github/workflows/cloudsigma-up.yaml'
        and run['actor']['login']=='Ariznawlll'
        and run['display_title']==f'up cs-component / {environment} / node-cache-replicate','copy run authority differs')
    artifact=api(f'repos/matrixorigin/cloudsigma-gitops/actions/artifacts/{row["artifact_id"]}')
    c.require(artifact['digest']==row['artifact_digest'] and artifact['expired'] is False
        and artifact['workflow_run']['id']==row['run_id'] and artifact['workflow_run']['head_sha']==row['gitops_sha'],
        'copy evidence artifact authority differs')
    source=next(n for n in catalog['nodes'] if n['environment']==environment and n['node']=='mo-standard-0')
    for entry in base.replication.entries_for(catalog,source):
        c.require(base.n.reviewed_entries({'entries':[entry]},entry['runtime'],api)==[entry],'compile producer authority differs')
    base.tools.authorize(lock,api(f'repos/{lock["producer"]["repository"]}/actions/runs/{lock["producer"]["id"]}'),
        api(f'repos/{lock["producer"]["repository"]}/actions/artifacts/{lock["artifact"]["id"]}'))


class Session(sessions.Session):
    def __init__(self,policy,config,state,path):
        super().__init__(policy,config,state,path,[])
        self.templates={};self.script=None
    def snapshot(self,node):
        value=super().snapshot(node)
        value['quotas']=self.call(['get','resourcequotas','-n','cicd','-o','json'])
        return value
    def guard(self,node,claims=0,pods=0):
        before=self.baselines.get(node)
        proof=admission(self.policy,self.snapshot(node),node,self.owned(),None if before is None else before['workloads'],claims,pods)
        c.require(before is None or proof['storage_class_uid']==before['storage_class_uid'],'workspace storage class replaced')
        return proof
    def cache_volume(self,binding):
        node=binding['node'];name=f'cs-{self.policy["environment"]}-incremental-n{node[-1]}-v1'
        expected={k:binding[k] for k in ('environment','node','node_uid','pvc_uid','pv_uid')}
        return self.bound(node,name,expected)
    def create_probe(self,template,node,binding):
        name=template['metadata']['name']
        c.require(self.call(['get','pvc',name+'-work','-n','cicd','--ignore-not-found','-o','json']) is None,
            'probe workspace already exists; reconcile')
        self.cache_volume(binding);self.guard(node,1,1)
        self.templates[name]=template;self.create(template)
        deadline=time.monotonic()+600
        while True:
            row=next(r for r in self.state['created'] if r['name']==name)
            pod=self.call(['get','pod',name,'-n','cicd','-o','json'])
            if checked_pod(template,pod,row['uid']):
                c.require(pod['spec']['nodeName']==node,'probe scheduled to wrong node')
                pvc=self.call(['get','pvc',name+'-work','-n','cicd','-o','json'])
                pv=self.call(['get','pv',pvc['spec']['volumeName'],'-o','json'])
                proof=private_volume(pod,pvc,pv,node,binding)
                proof['pv_name']=pv['metadata']['name'];proof['pod_name']=name
                self.state.setdefault('workspaces',[]).append(proof);self.journal();return proof
            c.require(time.monotonic()<deadline,'probe Pending provisioning/scheduling timeout; preserve for diagnosis')
            time.sleep(2)
    def invoke(self,workspace,binding,mode='probe'):
        # Recheck real identity/readiness immediately before execution.
        cm=self.call(['get','configmap',self.script['name'],'-n','cicd','-o','json'])
        c.require(cm['metadata']['uid']==self.script['uid'] and cm.get('immutable') is True
            and base.replication.sha(c.canonical(cm['data']))==self.script['data_sha256'],'probe instructions replaced')
        pod=self.call(['get','pod',workspace['pod_name'],'-n','cicd','-o','json'])
        c.require(checked_pod(self.templates[workspace['pod_name']],pod,workspace['pod_uid']),'probe no longer Ready')
        args=['exec',workspace['pod_name'],'-n','cicd','--','python3','/probe/node_cache_workspace_probe_job.py',mode,
            '--catalog','/probe/catalog.json','--lock','/probe/lock.json','--binding-json',c.canonical(binding).decode(),
            '--nonce',workspace['pod_uid']]
        start=pub.now();result=self.call(args,timeout=900)
        if mode=='probe':
            c.require(result['success'] is True and result['auxiliary_probe_only'] is True
                and result['binding']==binding and result['cpu_compatible'] is True
                and result['cache_write_denied_errno']=='EROFS'
                and result['compiler_invocations']==result['external_tool_downloads']==0
                and result['persistent_tools']['persistent_tools'] is True
                and result['persistent_tools']['external_tool_downloads']==0
                and result['persistent_tools']['binding']==binding
                and result['private_work']['private_marker_verified'] is True
                and result['private_work']['tmp_on_work_filesystem'] is True,'probe runtime proof failed')
        else:
            c.require(mode=='recheck-private' and result['private_marker_verified'] is True
                and result['tmp_on_work_filesystem'] is True,'private workspace peer recheck failed')
        return {'started_at':start.isoformat(),'completed_at':pub.now().isoformat(),'proof':result}
    def collect_workspace(self,workspace,node):
        self.guard(node)
        self.delete({'kind':'Pod','name':workspace['pod_name'],'uid':workspace['pod_uid']},'deleted')
        deadline=time.monotonic()+300
        while True:
            pod=self.call(['get','pod',workspace['pod_name'],'-n','cicd','--ignore-not-found','-o','json'])
            pvc=self.call(['get','pvc',workspace['pvc_name'],'-n','cicd','--ignore-not-found','-o','json'])
            pv=self.call(['get','pv',workspace['pv_name'],'--ignore-not-found','-o','json'])
            for obj,uid in [(pod,workspace['pod_uid']),(pvc,workspace['pvc_uid']),(pv,workspace['pv_uid'])]:
                c.require(obj is None or obj['metadata']['uid']==uid,'owned probe resource replaced during GC')
            if pod is pvc is pv is None:
                self.state.setdefault('workspace_gc',[]).append(workspace);self.journal();return
            c.require(time.monotonic()<deadline,'automatic workspace claim/PV GC not completed; do not delete PVC manually')
            time.sleep(2)


def run(policy,catalog,lock,evidence,config,directory,api=base.n.legacy.api):
    c.require(not directory.exists(),'probe intent already exists; reconcile without retry')
    environment=policy['environment'];c.require(environment in planner.ENVIRONMENTS,'unapproved environment')
    c.require(set(policy['node_uids'])==set(planner.NODES),'probe policy must bind exactly the three standard nodes')
    directory.mkdir();state={'environment':environment,'complete':False,'auxiliary_probe_only':True,
        'compiler_invocations':0,'external_tool_downloads':0,'started_at':pub.now().isoformat(),
        'created':[],'deleted':[],'targets':{},'automatic_retry':False}
    session=Session(policy,config,state,directory/'state.json')
    # MTY r1 completed both first-node readonly proofs but its inherited COPY
    # free-space check prevented owner-GC. A separate reviewed intent must not
    # collide with or overwrite the retained r1 ConfigMap/evidence.
    cm=f'cs-{environment}-allnode-work-proof-'+('r2' if environment in ('mty','jed') else 'r1')
    try:
        session.journal();authenticate(catalog,lock,evidence,environment,api)
        state['provenance_verified_before_write']=True
        accepted=next(r for r in evidence['copies'] if r['environment']==environment)['targets']['mo-standard-1']
        state['expected_payload']={k:accepted[k] for k in ('files','bytes')}
        original=session.call(['get','autoscalingrunnerset',f'cs-{environment}-large-7c14g','-n','cicd','-o','json'])
        original_id={'uid':original['metadata']['uid'],'spec_sha256':base.replication.sha(c.canonical(original['spec']))}
        state['original_pool']=original_id
        initial={node:session.snapshot(node) for node in planner.NODES}
        plans=planner.plan(catalog,policy,original,initial['mo-standard-0']['sc'],work_size_gib=WORK_GIB)
        slots={node:auxiliary_slots(initial[node],node) for node in planner.NODES}
        state['auxiliary_slots_by_node']=slots
        session.baselines={node:admission(policy,initial[node],node,(),None,slots[node],slots[node]) for node in planner.NODES}
        state['preflight']=session.baselines
        bindings={n['node']:n for n in catalog['nodes'] if n['environment']==environment}
        for binding in bindings.values():session.cache_volume(binding)
        files=base.FILES+('node_cache_workspace_probe_job.py',)
        data={name:(base.HERE/name).read_text() for name in files}
        data.update({'catalog.json':c.canonical(catalog).decode(),'lock.json':c.canonical(lock).decode()})
        c.require(sum(len(k.encode())+len(v.encode()) for k,v in data.items())<900000,'probe ConfigMap budget')
        session.guard('mo-standard-0')
        session.create({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':cm,'namespace':'cicd'},'immutable':True,'data':data})
        session.script={'name':cm,'uid':state['created'][0]['uid'],'data_sha256':base.replication.sha(c.canonical(data))}
        for planned in plans:
            node=planned['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname'];binding=bindings[node]
            prefix=cm+'-n'+node[-1];workspaces=[]
            state['targets'][node]={'binding':binding,'probes':[],'simultaneous_workspaces':slots[node]};session.journal()
            if slots[node]==1:
                # Leave reviewed reserve untouched. The next private workspace
                # is queued in this journal, not provisioned into unsafe space.
                state['targets'][node]['capacity_wait']={'reason':'physical_workspace_reservation',
                    'queued_at':pub.now().isoformat(),'runner_count_limit':False}
                first=session.create_probe(probe_pod(planned,prefix+'-a',cm),node,binding)
                first_result=session.invoke(first,binding)
                first_recheck=session.invoke(first,binding,'recheck-private')
                state['targets'][node]['queued_completed_proofs']=[{'workspace':first,**first_result}];session.journal()
                session.collect_workspace(first,node)
                state['targets'][node]['capacity_wait']['released_at']=pub.now().isoformat();session.journal()
                second=session.create_probe(probe_pod(planned,prefix+'-b',cm),node,binding)
                second_result=session.invoke(second,binding)
                second_recheck=session.invoke(second,binding,'recheck-private')
                state['targets'][node]['queued_completed_proofs'].append({'workspace':second,**second_result});session.journal()
                session.collect_workspace(second,node)
                workspaces=[first,second];results=[first_result,second_result]
                rechecks=[first_recheck,second_recheck]
            else:
                for suffix in ('a','b'):
                    workspaces.append(session.create_probe(probe_pod(planned,prefix+'-'+suffix,cm),node,binding))
                barrier=threading.Barrier(2)
                def execute(workspace):barrier.wait(timeout=30);return session.invoke(workspace,binding)
                with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(execute,workspaces))
                starts=[dt.datetime.fromisoformat(r['started_at']) for r in results]
                finishes=[dt.datetime.fromisoformat(r['completed_at']) for r in results]
                c.require(max(starts)<min(finishes),'probe execution did not actually overlap')
                rechecks=[session.invoke(w,binding,'recheck-private') for w in workspaces]
            c.require(len({w['pvc_uid'] for w in workspaces})==2 and len({w['pv_uid'] for w in workspaces})==2,
                'concurrent probes share a writable claim')
            for workspace,result in zip(workspaces,results):
                c.require(result['proof']['success'] and result['proof']['compiler_invocations']==result['proof']['external_tool_downloads']==0,
                    'auxiliary probe performed compilation/download')
                c.require(result['proof']['shared_payload_files']==accepted['files']
                    and result['proof']['shared_payload_bytes']==accepted['bytes'],'probe payload differs from audited copies')
                state['targets'][node]['probes'].append({'workspace':workspace,**result})
            session.journal()
            state['targets'][node]['peer_rechecks']=rechecks
            if slots[node]==2:session.collect_workspace(workspaces[0],node)
            replacement=session.create_probe(probe_pod(planned,prefix+'-replacement',cm),node,binding)
            c.require(replacement['pvc_uid'] not in {w['pvc_uid'] for w in workspaces},'replacement reused private workspace')
            proof=session.invoke(replacement,binding)
            c.require(proof['proof']['binding']==results[0]['proof']['binding']==binding,'replacement cache identity changed')
            c.require(proof['proof']['shared_payload_files']==accepted['files']
                and proof['proof']['shared_payload_bytes']==accepted['bytes'],'replacement payload differs from audited copies')
            state['targets'][node]['replacement']={'workspace':replacement,**proof};session.journal()
            if slots[node]==2:session.collect_workspace(workspaces[1],node)
            session.collect_workspace(replacement,node)
            session.cache_volume(binding);state['targets'][node]['postflight']=session.guard(node);session.journal()
        after=session.call(['get','autoscalingrunnerset',f'cs-{environment}-large-7c14g','-n','cicd','-o','json'])
        c.require(after['metadata']['uid']==original_id['uid'] and base.replication.sha(c.canonical(after['spec']))==original_id['spec_sha256'],
            'original runner pool changed during auxiliary validation')
        state['postflight']={node:session.guard(node) for node in planner.NODES}
        c.require(len(state.get('workspace_gc',[]))==9 and len(state['deleted'])==9,'probe cleanup incomplete')
        state.update(complete=True,helper_pods_absent=True,work_claims_collected=True,completed_at=pub.now().isoformat())
        session.journal();return state
    except Exception as error:
        state.update(error_type=type(error).__name__,failed_at=pub.now().isoformat(),
            failure_sites=[{'file':Path(f.filename).name,'line':f.lineno} for f in traceback.extract_tb(error.__traceback__)
                if Path(f.filename).name.startswith('node_cache_')])
        session.journal();raise


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for key in ('policy','catalog','lock','evidence','kubeconfig','directory'):parser.add_argument('--'+key,type=Path,required=True)
    a=parser.parse_args()
    try:run(c.read_json(a.policy),c.read_json(a.catalog),c.read_json(a.lock),c.read_json(a.evidence),a.kubeconfig,a.directory)
    except Exception as error:
        print(json.dumps({'complete':False,'error_type':type(error).__name__,'automatic_retry':False}));raise SystemExit(1)
