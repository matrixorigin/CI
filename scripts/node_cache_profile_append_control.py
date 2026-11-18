"""Scoped MTY CPU-repair publisher: new CM/Pod + private owner-GC workspace.

No cache PVC creation, runner mutation, compilation, catalog activation or retry.
Requires an independently reviewed successful producer which does not yet exist
for this repair; implementing this controller does not authorize a dispatch.
"""
import argparse
import copy
import datetime as dt
import json
from pathlib import Path
import time
import traceback

import incremental_cache as c
import node_cache_profile_append as core
import node_cache_publish_control as initial
import node_cache_publish_job as authorization
import node_cache_transport as transport
import node_cache_workspace_probe_control as q

CPU_RUN=44311269341
CPU_REV='734634f25ea51f2c452788a9d1cb43544f127f6d'
CPU_ARTIFACT=14430521392
CPU_DIGEST='sha256:2f54eaf7210c0d9eabeaed68646a9c44a542ab03a1ee0a522b356798e45ff8c7'
PROFILES={'mo-standard-1':'36928e1c2d96ed3c0a80a3f3b29eaeb068634bd5cf0d6ec102767901bd134499',
          'mo-standard-2':'8c424b925b5cb7938a3bad9c724b821753b2ffecea631768e5345579c90dab7e'}
SCRIPT='node_cache_profile_append_job.py'
FILES=tuple(sorted(set(initial.FILES+q.base.FILES+(
    'node_cache_append.py','node_cache_append_job.py','node_cache_profile_append.py',SCRIPT,
    'node_cache_cpu_inventory.py','node_cache_workspace_probe_job.py'))))


def scope(policy,catalog,entry,base):
    core.check_approval(base,entry)
    node=base['binding']['node'];env=policy['environment']
    c.require(env=='mty' and node in PROFILES and base['binding']['environment']==env,'CPU repair scope differs')
    c.require(core.cpus.checked_cpu(entry['runtime']['cpu'])==PROFILES[node],'CPU repair profile not independently reviewed')
    rows=[b for b in catalog['nodes'] if b['environment']==env and b['node']==node]
    c.require(len(rows)==1,'CPU repair cache binding missing')
    binding=rows[0]
    c.require(base['binding']=={k:binding[k] for k in base['binding']}
        and base['marker_sha256']==binding['marker_sha256']
        and policy['node_uids'][node]==binding['node_uid'],'CPU repair volume authority differs')
    c.require(core.cache.catalog_for_node(catalog,binding)['entries']==base['existing_entries'],'CPU base is not the full published catalog inventory')
    name=f"cs-{env}-nc-profile-n{node[-1]}-{entry['producer']['id']}-{entry['artifact']['id']}"
    c.require(len(name)<=63 and all(type(entry[k]['id']) is int and entry[k]['id']>0 for k in ('producer','artifact')),'CPU operation name differs')
    return binding,name


def cpu_authority(api):
    run=api(f'repos/matrixorigin/cloudsigma-gitops/actions/runs/{CPU_RUN}')
    c.require(run['id']==CPU_RUN and run['head_sha']==CPU_REV and run['head_branch']=='codex/ruh-pvc-readiness'
        and run['event']=='workflow_dispatch' and run['run_attempt']==1 and run['status']=='completed'
        and run['conclusion']=='success' and run['actor']['login']==run['triggering_actor']['login']=='Ariznawlll'
        and run['path']=='.github/workflows/cloudsigma-up.yaml'
        and run['display_title']=='up cs-component / mty / node-cache-cpu-inventory','CPU census run authority differs')
    artifact=api(f'repos/matrixorigin/cloudsigma-gitops/actions/artifacts/{CPU_ARTIFACT}')
    c.require(artifact['id']==CPU_ARTIFACT and artifact['digest']==CPU_DIGEST and artifact['expired'] is False
        and artifact['workflow_run']['id']==CPU_RUN and artifact['workflow_run']['head_sha']==CPU_REV,
        'CPU census artifact authority differs')


def fresh(entry,base,api):
    def metadata(value):
        p=value['producer'];a=value['artifact']
        result={'run':api(f"repos/{p['repository']}/actions/runs/{p['id']}"),
            'artifact':api(f"repos/{p['repository']}/actions/artifacts/{a['id']}"),
            'checked_at':initial.now().isoformat(),'binding':base['binding']}
        authorization.authorize(value,result)
        return result
    result=metadata(entry)
    result['parent_authorizations']=[{'index_sha256':p['index_sha256'],**metadata(p)} for p in base['existing_entries']]
    # Do not restamp older observations after collecting the other parents.
    authorization.authorize(entry,result)
    for parent,proof in zip(base['existing_entries'],result['parent_authorizations']):authorization.authorize(parent,proof)
    return result


def instructions(entry,base):
    data={name:(initial.HERE/name).read_text() for name in FILES}
    data.update({'reviewed-entry.json':c.canonical(entry).decode(),'append-base.json':c.canonical(base).decode()})
    c.require(sum(len(k.encode())+len(v.encode()) for k,v in data.items())<900000,'CPU publication ConfigMap budget')
    return data


def publisher_pod(planned,name,cm):
    pod=q.probe_pod(planned,name,cm);spec=pod['spec'];container=spec['containers'][0]
    # This is the separately authorized writer, NEVER an ordinary PR consumer.
    # Kubernetes omits these non-pointer bools when false. Author the canonical
    # writable default, not explicit false which cannot survive an API roundtrip.
    spec['volumes'][1]['persistentVolumeClaim'].pop('readOnly')
    container['volumeMounts'][1].pop('readOnly')
    container['resources']={'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'1','memory':'1Gi'}}
    container['env'] += [{'name':'ACTUAL_NODE','valueFrom':{'fieldRef':{'fieldPath':'spec.nodeName'}}},
                         {'name':'PYTHONDONTWRITEBYTECODE','value':'1'}]
    # Download/unpack stays on the separately bounded private workspace, not
    # root emptyDir. Init writes only the own workspace, never the cache.
    init=spec['initContainers'][0]
    c.require(init['command'][-1]=='mkdir -p /work/.runner-private-tmp; chmod 1777 /work/.runner-private-tmp','workspace init contract differs')
    init['command'][-1]+='; mkdir -p /work/.profile-stage'
    container['volumeMounts'].append({'name':'work','mountPath':'/stage','subPath':'.profile-stage'})
    return pod


def checked_publisher_pod(expected,actual,uid):
    ready=q.checked_pod(expected,actual,uid)
    spec=actual['spec']
    c.require(spec['volumes'][1]['persistentVolumeClaim'].get('readOnly',False) is False
        and spec['containers'][0]['volumeMounts'][1].get('readOnly',False) is False,
        'separately authorized publisher cache mount is not writable')
    return ready


def recovery_scope(approval,entry,name):
    """Only the independently inspected pre-stage API-normalization failure."""
    c.require(approval['schema']==1 and approval['run_id']==44338220751
        and approval['gitops_revision']=='5dacdfb7d951c70c42db216606672f7dd9d61586',
        'unreviewed CPU publisher recovery')
    old=approval['receipt']
    c.require(core.cache.hash_value(old)==approval['canonical_report_sha256']
        and old['complete'] is False and old['error_type']=='ValueError' and old['name']==name
        and old['node']=='mo-standard-1' and entry['producer']['id']==44326799783
        and old['failure_sites']==[{'file':'node_cache_profile_append_control.py','line':126},
            {'file':'node_cache_workspace_probe_control.py','line':203},
            {'file':'node_cache_workspace_probe_control.py','line':53},
            {'file':'incremental_cache.py','line':29}]
        and not set(old)&{'pending_create','pending_delete','active_phase','workspace','stage','append','verify'}
        and old['provenance_verified_before_write'] is True and old['deleted']==[]
        and [r['kind'] for r in old['created']]==['ConfigMap','Pod']
        and [r['name'] for r in old['created']]==[name,name+'-writer'],
        'publisher had progressed beyond the reviewed pre-stage failure')
    c.require(approval['artifact']=={'id':14442721595,'name':'node-cache-profile-append-mty-44338220751',
        'size_in_bytes':2598,'digest':'sha256:64478db06c8f1a9d94f67cde6c555975eec9e3d1a96f45124bf9b7b28952807f'},
        'failed publication artifact differs')
    return old


def recovery_authority(approval,entry,name,api):
    old=recovery_scope(approval,entry,name)
    run=api('repos/matrixorigin/cloudsigma-gitops/actions/runs/44338220751')
    c.require(run['head_sha']==approval['gitops_revision'] and run['status']=='completed'
        and run['conclusion']=='failure' and run['run_attempt']==1 and run['event']=='workflow_dispatch'
        and run['head_branch']=='codex/ruh-pvc-readiness' and run['path']=='.github/workflows/cloudsigma-up.yaml'
        and run['actor']['login']==run['triggering_actor']['login']=='Ariznawlll'
        and run['display_title']=='up cs-component / mty / node-cache-profile-append',
        'failed protected run authority differs')
    artifact=api('repos/matrixorigin/cloudsigma-gitops/actions/artifacts/14442721595')
    c.require(all(artifact[k]==v for k,v in approval['artifact'].items()) and artifact['expired'] is False
        and artifact['workflow_run']['id']==44338220751
        and artifact['workflow_run']['head_sha']==approval['gitops_revision'],'failed raw receipt authority differs')
    return old


def run(policy,catalog,lock,evidence,entry,base,config,directory,api=q.base.n.legacy.api,recovery=None,operation=None):
    c.require(not directory.exists(),'CPU publication intent exists; reconcile, never retry')
    # Default CPU-repair scope remains unchanged. A different protected
    # operation must supply its own independently reviewed scope and copied
    # base authentication; there is no CLI/user-input override of these hooks.
    c.require(operation is None or recovery is None,'native recovery requires a separately reviewed design')
    binding,name=(scope(policy,catalog,entry,base) if operation is None else operation.scope(policy,catalog,entry,base))
    node=binding['node'];profiles=PROFILES if operation is None else operation.profiles
    directory.mkdir();state={'environment':'mty','node':node,'name':name,'complete':False,
        'created':[],'deleted':[],'automatic_retry':False,'started_at':initial.now().isoformat(),
        'compiler_invocations':0,'existing_cache_pvc_preserved':True,'catalog_activated':False,
        'cpu_profile_sha256':profiles[node],'cpu_inventory_run':CPU_RUN,'targets':{}}
    if operation is not None:state.update(native_only=True,native_key=next(iter(entry['native_manifests'])),source=entry['source'])
    session=q.Session(policy,config,state,directory/'state.json')
    try:
        if recovery is not None:
            old=recovery_authority(recovery,entry,name,api)
            state['created']=copy.deepcopy(old['created'])
            state['adopted_resources']=copy.deepcopy(old['created'])
            state['recovery_of']={'run_id':recovery['run_id'],'canonical_report_sha256':recovery['canonical_report_sha256'],
                'pre_stage_failure_only':True,'new_pvcs':0,'new_pods':0}
        session.journal()
        if operation is None:q.authenticate(catalog,lock,evidence,'mty',api)
        else:operation.authenticate(catalog,lock,evidence,api)
        cpu_authority(api);fresh(entry,base,api)
        state['provenance_verified_before_write']=True
        original=session.call(['get','autoscalingrunnerset','cs-mty-large-7c14g','-n','cicd','-o','json'])
        original_identity={'uid':original['metadata']['uid'],'spec_sha256':core.cache.hash_value(original['spec'])}
        state['original_pool']=original_identity
        snapshots={n:session.snapshot(n) for n in q.planner.NODES}
        session.baselines={n:q.admission(policy,snapshots[n],n,session.owned(),None,
            int(n==node and recovery is None),int(n==node and recovery is None)) for n in q.planner.NODES}
        state['preflight']=session.baselines;session.cache_volume(binding)
        planned=next(p for p in q.planner.plan(catalog,policy,original,snapshots[node]['sc'],work_size_gib=q.WORK_GIB)
            if p['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname']==node)
        data=instructions(entry,base) if operation is None else operation.instructions(entry,base)
        script=SCRIPT if operation is None else operation.script
        session.guard(node)
        if recovery is None:
            session.create({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':'cicd'},
                'immutable':True,'data':data})
        cm={'uid':state['created'][0]['uid'],'sha256':core.cache.hash_value(data)}
        template=publisher_pod(planned,name+'-writer',name)
        if recovery is None:workspace=session.create_probe(template,node,binding)
        else:
            current=session.call(['get','configmap',name,'-n','cicd','-o','json'])
            c.require(current['metadata']['uid']==cm['uid'] and current['immutable']
                and core.cache.hash_value(current['data'])==cm['sha256'],'adopted publisher instructions changed')
            pod=session.call(['get','pod',name+'-writer','-n','cicd','-o','json'])
            c.require(checked_publisher_pod(template,pod,state['created'][1]['uid']),'adopted publisher not Ready/exact')
            elapsed=(initial.now()-dt.datetime.fromisoformat(pod['metadata']['creationTimestamp'].replace('Z','+00:00'))).total_seconds()
            c.require(0<=elapsed<=900 and pod['spec']['activeDeadlineSeconds']==3600,
                'existing helper lifetime insufficient; preserve and design a fresh scoped recovery')
            pvc=session.call(['get','pvc',name+'-writer-work','-n','cicd','-o','json'])
            pv=session.call(['get','pv',pvc['spec']['volumeName'],'-o','json'])
            workspace={**q.private_volume(pod,pvc,pv,node,binding),'pod_name':name+'-writer','pv_name':pv['metadata']['name']}
            session.templates[workspace['pod_name']]=template;state['workspaces']=[workspace]
            empty=session.call(['exec',workspace['pod_name'],'-n','cicd','--','python3','-c',
                'import json;from pathlib import Path;print(json.dumps({"stage_empty":not any(Path("/stage").iterdir())}))'])
            c.require(empty=={'stage_empty':True},'unexpected staged data; preserve rather than overwrite')
            state['recovery_of']['empty_private_stage_verified']=True
        state['workspace']=workspace;session.journal()
        for phase in ('stage','append','verify'):
            session.cache_volume(binding);state['latest_resources']=session.guard(node)
            current=session.call(['get','configmap',name,'-n','cicd','-o','json'])
            c.require(current['metadata']['uid']==cm['uid'] and current['immutable']
                and core.cache.hash_value(current['data'])==cm['sha256'],'CPU publisher instructions changed')
            pod=session.call(['get','pod',workspace['pod_name'],'-n','cicd','-o','json'])
            c.require(checked_publisher_pod(session.templates[workspace['pod_name']],pod,workspace['pod_uid']),'CPU publisher no longer Ready')
            pvc=session.call(['get','pvc',workspace['pvc_name'],'-n','cicd','-o','json'])
            pv=session.call(['get','pv',workspace['pv_name'],'-o','json'])
            actual=q.private_volume(pod,pvc,pv,node,binding)
            c.require(all(actual[k]==workspace[k] for k in actual),'CPU private stage identity changed')
            request=fresh(entry,base,api)
            if phase=='stage':request['url']=transport.signed_archive_url(entry)
            state['active_phase']=phase;session.journal();started=time.monotonic()
            result=session.call(['exec','-i',workspace['pod_name'],'-n','cicd','--','python3','/probe/'+script,phase],request,3700)
            c.require(result['success'] is True and result['phase']==phase and result['compiler_invocations']==0,'CPU publisher phase failed')
            state[phase]={**result,'controller_seconds':round(time.monotonic()-started,3)};session.journal()
        c.require(state['verify']['independently_verified'] and state['verify']['compatibility_aliases'] is False
            and state['verify']['binding']==base['binding'] and state['verify']['marker_sha256']==base['marker_sha256'],
            'CPU independent verification differs')
        session.collect_workspace(workspace,node);session.cache_volume(binding)
        after=session.call(['get','autoscalingrunnerset','cs-mty-large-7c14g','-n','cicd','-o','json'])
        c.require(after['metadata']['uid']==original_identity['uid']
            and core.cache.hash_value(after['spec'])==original_identity['spec_sha256'],'original pool changed')
        state['postflight']={n:session.guard(n) for n in q.planner.NODES}
        c.require([r['kind'] for r in state['created']]==['ConfigMap','Pod'] and state['deleted']==state['created'][1:]
            and len(state.get('workspace_gc',[]))==1,'CPU publisher/temporary workspace cleanup incomplete')
        state.update(complete=True,helper_pods_absent=True,private_stage_collected=True,completed_at=initial.now().isoformat())
        state.pop('active_phase');session.journal();return state
    except Exception as error:
        state.update(error_type=type(error).__name__,failed_at=initial.now().isoformat(),
            failure_sites=[{'file':Path(f.filename).name,'line':f.lineno} for f in traceback.extract_tb(error.__traceback__)
                if Path(f.filename).name.startswith(('node_cache_','incremental_','publish_node_'))])
        session.journal();raise


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('policy','catalog','lock','evidence','entry','base','kubeconfig','directory'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--recovery',type=Path)
    a=parser.parse_args()
    try:run(c.read_json(a.policy),c.read_json(a.catalog),c.read_json(a.lock),c.read_json(a.evidence),
        c.read_json(a.entry),c.read_json(a.base),a.kubeconfig,a.directory,
        recovery=None if a.recovery is None else c.read_json(a.recovery))
    except Exception as error:
        print(json.dumps({'complete':False,'error_type':type(error).__name__,'automatic_retry':False}));raise SystemExit(1)
