"""Create-only all-node new reader registration; never mutate old sets/cache.

Only approved absent AutoscalingRunnerSets can be created, each once after a
fresh complete gate. Any partial/unknown response is preserved for read-only
reconciliation. No dispatch, cache copy, PVC creation/deletion or compiler.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
import traceback

import incremental_cache as c
import node_compile_cache as cache
import node_cache_reader_rollout as fleet
import node_cache_workspace_probe_control as q


class Session(q.Session):
    def owned(self):
        return super().owned()+[v['listener_pod_uid'] for v in self.state.get('listeners',{}).values()]


def snapshots(session):
    with ThreadPoolExecutor(max_workers=3) as executor:
        values=executor.map(session.snapshot,fleet.plan.NODES)
        return dict(zip(fleet.plan.NODES,values))


def observe_listener(session,resource,row):
    name=row['name'];deadline=time.monotonic()+600
    while True:
        live=session.call(['get','autoscalingrunnerset',name,'-n','cicd','-o','json'])
        c.require(live['metadata']['uid']==row['uid'] and live['spec']==resource['spec'],
            'new reader UID/spec changed; preserve and reconcile')
        listeners=session.call(['get','autoscalinglisteners','-n','cicd','-o','json'])['items']
        matches=[v for v in listeners if v['spec'].get('autoscalingRunnerSetName')==name
            and v['spec'].get('autoscalingRunnerSetNamespace')=='cicd']
        c.require(len(matches)<=1,'ambiguous reader listener')
        if matches:
            listener=matches[0];pods=session.call(['get','pods','-n','cicd','-o','json'])['items']
            owned=[v for v in pods if any(o.get('kind')=='AutoscalingListener'
                and o.get('uid')==listener['metadata']['uid'] for o in v['metadata'].get('ownerReferences',[]))]
            c.require(len(owned)<=1,'ambiguous reader listener Pod')
            if owned and owned[0]['status'].get('phase')=='Running' and any(
                    x['type']=='Ready' and x['status']=='True' for x in owned[0]['status'].get('conditions',[])):
                return {**fleet.capacity.listener_identity(resource,live,listener,owned[0]),'pr_route_enabled':True}
        c.require(time.monotonic()<deadline,'reader Pending; preserve journal, never recreate')
        time.sleep(2)


def run(policy,base,lock,evidence,cpu_review,config,directory,api):
    c.require(not directory.exists(),'reader intent exists; reconcile, never repeat create')
    env=policy['environment'];c.require(env in ('jed','mnl2','ruh'),'new all-node rollout excludes completed MTY reader sets')
    cpus=fleet.cpu_inventory(cpu_review,base,env,api)
    authority=fleet.authenticate(base,lock,evidence,env,list(fleet.plan.NODES),cpus,api)
    # No cluster mutation has occurred if producer/copy/tool/census fails.
    directory.mkdir(mode=0o700)
    state={'environment':env,'target_nodes':list(fleet.plan.NODES),'registered_nodes':[],
        'complete':False,'created':[],'listeners':{},'automatic_retry':False,'cache_writes':0,
        'workflow_dispatched':False,'runtime_readers_accepted':False,'runner_count_cap':None,
        'authority':authority,'started_at':q.pub.now().isoformat(),'base_catalog_sha256':cache.hash_value(base)}
    session=Session(policy,config,state,directory/'state.json');session.journal()
    try:
        original=session.call(['get','autoscalingrunnerset',f'cs-{env}-large-7c14g','-n','cicd','-o','json'])
        state['original_pool']={'uid':original['metadata']['uid'],'spec_sha256':cache.hash_value(original['spec'])}
        initial=snapshots(session)
        session.baselines={n:q.admission(policy,s,n) for n,s in initial.items()};state['preflight']=session.baselines
        proposal=fleet.resources(base,policy,original,initial['mo-standard-0']['sc'],list(fleet.plan.NODES),cpus)
        state['desired']=proposal['resources'];resources=state['desired'];session.journal()
        existing=session.call(['get','autoscalingrunnersets','-n','cicd','-o','json'])['items']
        names={v['metadata']['name'] for v in resources}
        c.require(not any(v['metadata']['name'] in names or fleet.plan.demand.COMMON_ROUTE in v['spec'].get('runnerScaleSetLabels',[])
            or f'cs-{env}-node-cache-all' in v['spec'].get('runnerScaleSetLabels',[]) for v in existing),
            'reader name/route already exists; reconcile without overwriting')
        for resource in resources:
            fresh=snapshots(session)
            for node,snapshot in fresh.items():
                q.admission(policy,snapshot,node,session.owned(),session.baselines[node]['workloads'])
                session.cache_volume(next(v for v in base['nodes'] if (v['environment'],v['node'])==(env,node)))
            state['listener_registration_capacity']=fleet.admission(policy,resources,fresh,session.owned())
            current=session.call(['get','autoscalingrunnerset',original['metadata']['name'],'-n','cicd','-o','json'])
            c.require(current['metadata']['uid']==state['original_pool']['uid'] and current['spec']==original['spec'],
                'original pool changed before create')
            name=resource['metadata']['name']
            c.require(session.call(['get','autoscalingrunnerset',name,'-n','cicd','--ignore-not-found','-o','json']) is None,
                'reader appeared before create; reconcile')
            c.require(all(0<=(q.pub.now()-__import__('datetime').datetime.fromisoformat(
                s['stats']['node']['fs']['time'].replace('Z','+00:00'))).total_seconds()<=120 for s in fresh.values()),
                'reader fresh safety gate expired')
            state['pending_create']={'kind':'AutoscalingRunnerSet','name':name};session.journal()
            made=session.call(['create','-f','-','-o','json'],resource)
            c.require(made['kind']=='AutoscalingRunnerSet' and made['metadata']['name']==name
                and made['metadata']['namespace']=='cicd' and made['spec']==resource['spec'],
                'create response unknown/different; preserve, reconcile only')
            row={'kind':'AutoscalingRunnerSet','name':name,'uid':made['metadata']['uid']}
            state['created'].append(row);state.pop('pending_create');session.journal()
            state['listeners'][name]=observe_listener(session,resource,row)
            state['registered_nodes'].append(resource['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname'])
            session.journal()
        state['postflight']={n:session.guard(n) for n in fleet.plan.NODES}
        after=session.call(['get','autoscalingrunnerset',original['metadata']['name'],'-n','cicd','-o','json'])
        c.require(after['metadata']['uid']==state['original_pool']['uid'] and after['spec']==original['spec'],
            'original pool changed after registration')
        state.update(complete=True,completed_at=q.pub.now().isoformat());session.journal()
        return state
    except Exception as error:
        state.update(error_type=type(error).__name__,failure_sites=[{'file':Path(v.filename).name,'line':v.lineno}
            for v in traceback.extract_tb(error.__traceback__)],failed_at=q.pub.now().isoformat())
        session.journal();raise
