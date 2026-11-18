"""New readonly helpers only: CPU census without allocating private/cache PVCs."""
import argparse
import json
from pathlib import Path
import time
import traceback

import incremental_cache as c
import node_cache_workspace_probe_control as q
import node_cache_cpu_inventory as cpu


def run(policy,catalog,lock,evidence,config,directory,api=q.base.n.legacy.api):
    c.require(not directory.exists(),'CPU intent exists: reconcile, never retry')
    env=policy['environment'];c.require(env in q.planner.ENVIRONMENTS and set(policy['node_uids'])==set(q.planner.NODES),'CPU inventory policy differs')
    directory.mkdir();state={'environment':env,'complete':False,'readonly':True,'filesystem_writes':0,
        'compiler_invocations':0,'external_tool_downloads':0,'created':[],'deleted':[],
        'cpu_profile_only':True,'automatic_retry':False,'started_at':q.pub.now().isoformat(),'targets':{}}
    s=q.Session(policy,config,state,directory/'state.json');name=f'cs-{env}-cpu-inventory-r1'
    try:
        s.journal();q.authenticate(catalog,lock,evidence,env,api);state['provenance_verified_before_write']=True
        state['catalog_sha256']=q.base.replication.sha(c.canonical(catalog))
        original=s.call(['get','autoscalingrunnerset',f'cs-{env}-large-7c14g','-n','cicd','-o','json'])
        original_identity={'uid':original['metadata']['uid'],'spec_sha256':q.base.replication.sha(c.canonical(original['spec']))}
        state['original_pool']=original_identity
        s.baselines={n:q.admission(policy,s.snapshot(n),n) for n in q.planner.NODES};state['preflight']=s.baselines
        bindings={b['node']:b for b in catalog['nodes'] if b['environment']==env}
        for b in bindings.values():s.cache_volume(b)
        data={f:(q.base.HERE/f).read_text() for f in q.base.FILES+('node_cache_workspace_probe_job.py','node_cache_cpu_inventory.py')}
        data['catalog.json']=c.canonical(catalog).decode()
        c.require(sum(len(k.encode())+len(v.encode()) for k,v in data.items())<900000,'CPU instructions budget')
        s.guard('mo-standard-0');s.create({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':'cicd'},'immutable':True,'data':data})
        cmuid=state['created'][0]['uid']
        for node,binding in bindings.items():
            claim=f'cs-{env}-incremental-n{node[-1]}-v1';podname=name+'-n'+node[-1]
            template=q.base.helper(podname,node,claim,name,True)
            container=template['spec']['containers'][0]
            container['securityContext'].update(readOnlyRootFilesystem=True,runAsNonRoot=True)
            container['env']=[{'name':'PYTHONDONTWRITEBYTECODE','value':'1'}]
            s.cache_volume(binding);s.guard(node);s.create(template)
            row=state['created'][-1];deadline=time.monotonic()+300
            while True:
                actual=s.call(['get','pod',podname,'-n','cicd','-o','json'])
                if q.checked_pod(template,actual,row['uid']):
                    c.require(actual['spec']['nodeName']==node,'CPU helper node differs');break
                c.require(time.monotonic()<deadline,'CPU helper Pending; preserve journal without retry');time.sleep(2)
            cm=s.call(['get','configmap',name,'-n','cicd','-o','json'])
            c.require(cm['metadata']['uid']==cmuid and cm['immutable'] and cm['data']==data,'CPU instructions replaced')
            report=s.call(['exec',podname,'-n','cicd','--','python3','/probe/node_cache_cpu_inventory.py',
                '--catalog','/probe/catalog.json','--binding-json',c.canonical(binding).decode()],timeout=60)
            c.require(report['binding']==binding and report['cpu_sha256']==cpu.checked_cpu(report['cpu'])
                and report['readonly'] and report['filesystem_writes']==report['compiler_invocations']==report['external_tool_downloads']==0,
                'CPU runtime report differs')
            state['targets'][node]={'pod_uid':row['uid'],'observed_at':q.pub.now().isoformat(),'report':report};s.journal()
            s.cache_volume(binding);s.guard(node);s.delete(row,'deleted');s.absent([row])
        after=s.call(['get','autoscalingrunnerset',f'cs-{env}-large-7c14g','-n','cicd','-o','json'])
        c.require(after['metadata']['uid']==original_identity['uid']
            and q.base.replication.sha(c.canonical(after['spec']))==original_identity['spec_sha256'],'old runner pool changed')
        state['postflight']={n:s.guard(n) for n in q.planner.NODES}
        c.require([r['kind'] for r in state['created']]==['ConfigMap','Pod','Pod','Pod'] and state['deleted']==state['created'][1:],'CPU helper cleanup differs')
        state.update(complete=True,helper_pods_absent=True,completed_at=q.pub.now().isoformat());s.journal();return state
    except Exception as error:
        state.update(error_type=type(error).__name__,failed_at=q.pub.now().isoformat(),
            failure_sites=[{'file':Path(f.filename).name,'line':f.lineno} for f in traceback.extract_tb(error.__traceback__) if Path(f.filename).name.startswith('node_cache_')])
        s.journal();raise


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('policy','catalog','lock','evidence','kubeconfig','directory'):parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args()
    try:run(c.read_json(a.policy),c.read_json(a.catalog),c.read_json(a.lock),c.read_json(a.evidence),a.kubeconfig,a.directory)
    except Exception as error:
        print(json.dumps({'complete':False,'error_type':type(error).__name__,'automatic_retry':False}));raise SystemExit(1)
