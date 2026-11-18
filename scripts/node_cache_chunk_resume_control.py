"""Resume only explicitly reviewed failed chunks, never recreate either PVC."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import time
import traceback

import incremental_cache as c
import node_cache_replicate_control as b
import node_cache_partial_recover_control as partial

PRIOR=44279718678
PRIOR_REV='d949c3bf9ec01c87145e5a9d72ceb9fcf096fef8'
NAME='cs-jed-cache-chunks-r3'
RUH_PRIOR=44282517875
RUH_REV='5b80fcf0ac4a04aaca7453cce04ec6781057541e'


def scope(env):
    c.require(env in ('jed','ruh'),'chunk environment not approved')
    return (PRIOR,PRIOR_REV,NAME,'cs-jed-cache-partial-r2',3700,4500,2) if env=='jed' else (
        RUH_PRIOR,RUH_REV,'cs-ruh-cache-chunks-r2','cs-ruh-cache-partial-r1',7200,9000,18)


def checked_inspection(approval,old):
    proof=approval['inspection']
    c.require(approval['inspection_run_id']==44290243774 and proof['environment']=='ruh'
        and proof['inspected_run_id']==RUH_PRIOR and proof['complete'] is True
        and proof['readonly'] is True and proof['filesystem_writes']==0
        and proof['created']==proof['deleted']==[] and proof['compiler_invocations']==proof['external_downloads']==0
        and proof['targets']['mo-standard-1']==old['targets']['mo-standard-1']['verify'],
        'protected RUH inventory proof differs')
    rows=[r for r in old['created'] if r['kind']=='Pod']
    c.require(set(proof['helpers'])=={r['name'] for r in rows}
        and all(proof['helpers'][r['name']]['uid']==r['uid']
            and proof['helpers'][r['name']]['restarts']==0
            and proof['helpers'][r['name']]['memory_events']['oom_kill']==0 for r in rows),
        'RUH helpers have replacement/restart/OOM evidence')
    inv=proof['targets']['mo-standard-2']
    c.require(inv['success'] is True and inv['readonly'] is True and inv['filesystem_writes']==0
        and inv['batches']==36 and inv['acknowledged']==list(range(18))
        and inv['expected_files']==old['targets']['mo-standard-1']['verify']['files']
        and inv['expected_bytes']==old['targets']['mo-standard-1']['verify']['bytes']
        and inv['verified_bytes']>=sum(b['bytes'] for b in old['targets']['mo-standard-2']['batches'])
        and inv['acknowledged_receipts']==old['targets']['mo-standard-2']['batches'],
        'RUH retained chunk inventory differs')
    return proof


def approval_scope(approval,policy):
    old=approval['receipt'];control=approval['ack_control']
    env=policy['environment'];prior,rev,name,prefix,budget,lifetime,ack_count=scope(env)
    sites=[{'file':'node_cache_partial_recover_control.py','line':233 if env=='jed' else 257},
           {'file':'node_cache_replicate_control.py','line':116 if env=='jed' else 133}]
    c.require(approval['prior_run_id']==prior
        and old['environment']==env and old['complete'] is False and old['error_type']=='ValueError'
        and old['failure_sites']==sites
        and 'pending_create' not in old and old['deleted']==[]
        and old['targets']['mo-standard-1']['received']==old['targets']['mo-standard-1']['verify']
        and old['targets']['mo-standard-1']['verify']['success'] is True
        and [r['kind'] for r in old['created']]==['ConfigMap','Pod','Pod','PersistentVolumeClaim','Pod']
        and [r['name'] for r in old['created']]==[f'cs-{env}-cache-partial-r1',prefix+'-source',
            prefix+'-n1',f'cs-{env}-incremental-n2-v1',prefix+'-n2']
        and [r['number'] for r in old['targets']['mo-standard-2']['batches']]==list(range(ack_count)),
        'not the reviewed failed chunk transfer')
    c.require(control['readonly'] is True and control['filesystem_writes']==0 and control['producer_waited_for_ack'] is True
        and control['batch']==2 and control['verified_files']==556 and control['verified_bytes']==264928544,
        'ACK transport control differs')
    if env=='ruh':checked_inspection(approval,old)
    return old


class Session:
    def __init__(self,policy,config,state,path,adopted):
        self.policy=policy;self.config=config;self.state=state;self.path=path;self.adopted=adopted;self.baselines={}
    def journal(self):b.pub.write_journal(self.path,self.state)
    def helper(self,name,node,claim,cm,readonly):
        result=b.helper(name,node,claim,cm,readonly)
        lifetime=scope(self.policy['environment'])[5]
        if lifetime!=4500:
            result['spec']['activeDeadlineSeconds']=lifetime
            result['spec']['containers'][0]['command']=['/bin/bash','-euc','python3 --version; sleep '+str(lifetime-100)]
        return result
    def command(self,args):return ['kubectl','--kubeconfig',str(self.config),'--request-timeout=30s',*args]
    def call(self,args,data=None,timeout=45):
        p=subprocess.run(self.command(args),input=None if data is None else c.canonical(data).decode(),capture_output=True,text=True,timeout=timeout)
        c.require(p.returncode==0,'scoped chunk resume operation failed')
        return json.loads(p.stdout) if p.stdout.strip() else None
    def owned(self):return [r['uid'] for r in self.adopted+self.state['created'] if r['kind']=='Pod']
    def snapshot(self,node):
        return {k:self.call(a) for k,a in {'namespaces':['get','namespace','-o','json'],'nodes':['get','nodes','-o','json'],
            'pods':['get','pods','-A','-o','json'],'sc':['get','sc','directpv-min-io-ephemeral','-o','json'],
            'drives':['get','directpvdrives','-o','json'],'stats':['get','--raw','/api/v1/nodes/'+node+'/proxy/stats/summary']}.items()}
    def guard(self,node):
        baseline=self.baselines.get(node)
        return b.snapshot_guard(self.policy,self.snapshot(node),node,self.owned(),None if baseline is None else baseline['workloads'])
    def bound(self,node,claim,expected):
        pvc=self.call(['get','pvc',claim,'-n','cicd','-o','json']);pv=self.call(['get','pv',pvc['spec']['volumeName'],'-o','json'])
        return partial.volume(self.policy,node,claim,pvc,pv,expected)
    def create(self,obj):
        kind,name=obj['kind'],obj['metadata']['name'];c.require(kind in ('ConfigMap','Pod'),'chunk resume never creates PVCs')
        c.require(self.call(['get',kind,name,'-n','cicd','--ignore-not-found','-o','json']) is None,'resource exists: reconcile')
        self.state['pending_create']={'kind':kind,'name':name};self.journal();made=self.call(['create','-f','-','-o','json'],obj)
        self.state['created'].append({'kind':kind,'name':name,'uid':made['metadata']['uid']});self.state.pop('pending_create');self.journal()
    def delete(self,row,key):
        current=self.call(['get','pod',row['name'],'-n','cicd','-o','json']);c.require(current['metadata']['uid']==row['uid'],'owned helper replaced')
        self.call(['delete','--raw','/api/v1/namespaces/cicd/pods/'+row['name'],'-f','-'],
            {'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':row['uid']},'gracePeriodSeconds':1})
        self.state[key].append(row);self.journal()
    def absent(self,rows):
        deadline=time.monotonic()+120
        while True:
            if all(self.call(['get','pod',r['name'],'-n','cicd','--ignore-not-found','-o','json']) is None for r in rows):return
            c.require(time.monotonic()<deadline,'helper deletion not confirmed');time.sleep(2)
    def wait(self,name,node,claim,readonly):
        row=next(r for r in self.state['created'] if r['name']==name);deadline=time.monotonic()+240
        while True:
            p=self.call(['get','pod',name,'-n','cicd','-o','json'])
            cm=scope(self.policy['environment'])[2]
            c.require(p['metadata']['uid']==row['uid'] and b.checked_helper_spec(self.helper(name,node,claim,cm,readonly)['spec'],p['spec']),
                      'new helper template differs')
            if p.get('status',{}).get('phase')=='Running' and any(x['type']=='Ready' and x['status']=='True' for x in p['status'].get('conditions',[])):
                c.require(p['spec']['nodeName']==node,'helper node differs');return
            c.require(p.get('status',{}).get('phase') not in ('Failed','Succeeded') and time.monotonic()<deadline,'helper unready');time.sleep(2)


def run(policy,catalog,lock,approval,config,directory,api=b.n.legacy.api):
    c.require(not directory.exists(),'fresh chunk resume intent required');old=approval_scope(approval,policy)
    env=policy['environment'];prior_id,prior_rev,name,prefix,copy_budget,lifetime,ack_count=scope(env)
    directory.mkdir();state={'environment':env,'complete':False,'compiler_invocations':0,'automatic_retry':False,
        'started_at':b.pub.now().isoformat(),'resumed_chunk_run_id':prior_id,'adopted_resources':old['created'],
        'existing_pvcs_preserved':True,'node1_not_modified':True,'created':[],'retired_helpers':[],'deleted':[],'targets':{}}
    s=Session(policy,config,state,directory/'state.json',old['created'])
    source=next(n for n in catalog['nodes'] if n['environment']==env and n['node']=='mo-standard-0')
    def execute(pod,mode,target=None,number=None,selected=None):
        module='node_cache_replication.py' if mode=='verify' else 'node_cache_chunk_replication.py'
        args=partial.exec_args(pod,mode,target,module,number)
        if selected is not None:
            raw=c.canonical(selected).decode();c.require(len(raw.encode())<=100000,'batch selection argument budget')
            args+=['--selected-json',raw]
        return args
    try:
        s.journal();prior=api('repos/matrixorigin/cloudsigma-gitops/actions/runs/'+str(prior_id))
        c.require(prior['head_sha']==prior_rev and prior['head_branch']=='codex/ruh-pvc-readiness' and prior['event']=='workflow_dispatch'
            and prior['status']=='completed' and prior['conclusion']=='failure' and prior['run_attempt']==1
            and prior['path']=='.github/workflows/cloudsigma-up.yaml' and prior['actor']['login']=='Ariznawlll','prior chunk provenance differs')
        if env=='ruh':
            proof=api('repos/matrixorigin/cloudsigma-gitops/actions/runs/44290243774')
            c.require(proof['head_sha']=='41ef9445611c8b7b8eaa9510f64ac65dbe67207e'
                and proof['head_branch']=='codex/ruh-pvc-readiness' and proof['event']=='workflow_dispatch'
                and proof['status']=='completed' and proof['conclusion']=='success' and proof['run_attempt']==1
                and proof['path']=='.github/workflows/cloudsigma-up.yaml' and proof['actor']['login']=='Ariznawlll',
                'protected RUH inspection provenance differs')
            state['readonly_inspection_run_id']=44290243774
        for entry in b.replication.entries_for(catalog,source):
            c.require(b.n.reviewed_entries({'entries':[entry]},entry['runtime'],api)==[entry],'unreviewed cache producer')
        b.tools.authorize(lock,api(f'repos/{lock["producer"]["repository"]}/actions/runs/{lock["producer"]["id"]}'),
            api(f'repos/{lock["producer"]["repository"]}/actions/artifacts/{lock["artifact"]["id"]}'))
        state['provenance_verified_before_write']=True
        s.baselines={n:s.guard(n) for n in policy['node_uids']};state['preflight']=s.baselines;s.journal()
        source_id={k:source[k] for k in ('environment','node','node_uid','pvc_uid','pv_uid')}
        s.bound('mo-standard-0',policy['name'],source_id)
        for i in (1,2):s.bound(f'mo-standard-{i}',f'cs-{env}-incremental-n{i}-v1',old['targets'][f'mo-standard-{i}']['binding'])
        cm=s.call(['get','configmap',old['created'][0]['name'],'-n','cicd','-o','json'])
        c.require(cm['metadata']['uid']==old['created'][0]['uid'] and cm.get('immutable') is True,'old copy ConfigMap replaced')
        roles=[(old['created'][1],'mo-standard-0',policy['name'],True),
               (old['created'][2],'mo-standard-1',f'cs-{env}-incremental-n1-v1',False),
               (old['created'][4],'mo-standard-2',f'cs-{env}-incremental-n2-v1',False)]
        for row,node,claim,ro in roles:
            pod=s.call(['get','pod',row['name'],'-n','cicd','-o','json'])
            c.require(pod['metadata']['uid']==row['uid'] and b.checked_helper_spec(s.helper(row['name'],node,claim,old['created'][0]['name'],ro)['spec'],pod['spec']),
                      'old chunk helper scope differs')
        for row,node,_,_ in roles:s.guard(node);s.delete(row,'retired_helpers')
        s.absent(state['retired_helpers'])
        data={name:(b.HERE/name).read_text() for name in b.FILES+('node_cache_partial_replication.py','node_cache_chunk_replication.py')}
        data.update({'catalog.json':c.canonical(catalog).decode(),'lock.json':c.canonical(lock).decode(),'source.json':c.canonical(source).decode()})
        c.require(sum(len(k.encode())+len(v.encode()) for k,v in data.items())<900000,'chunk resume ConfigMap budget')
        s.guard('mo-standard-0');s.create({'apiVersion':'v1','kind':'ConfigMap','immutable':True,'metadata':{'name':name,'namespace':'cicd'},'data':data})
        src=name+'-source';p1=name+'-n1';p2=name+'-n2'
        for pod,node,claim,ro in [(src,'mo-standard-0',policy['name'],True),(p1,'mo-standard-1',f'cs-{env}-incremental-n1-v1',True),
                                   (p2,'mo-standard-2',f'cs-{env}-incremental-n2-v1',False)]:
            s.guard(node);s.create(s.helper(pod,node,claim,name,ro));s.wait(pod,node,claim,ro)
        for i in (1,2):s.bound(f'mo-standard-{i}',f'cs-{env}-incremental-n{i}-v1',old['targets'][f'mo-standard-{i}']['binding'])
        target1=old['targets']['mo-standard-1']['binding'];target2=old['targets']['mo-standard-2']['binding']
        verified=s.call(execute(p1,'verify',target1),timeout=600)
        c.require(verified==old['targets']['mo-standard-1']['verify'],'published node1 changed')
        state['targets']['mo-standard-1']={**copy.deepcopy(old['targets']['mo-standard-1']),'verify':verified,'readonly_reverified':True}
        inv=s.call(execute(p2,'inventory',target2),timeout=600)
        c.require(inv['success'] and inv['readonly'] and inv['filesystem_writes']==0
            and inv['expected_bytes']==verified['bytes'] and inv['expected_files']==verified['files']
            and set(range(ack_count))<=set(inv['acknowledged']),'retained node2 inventory differs')
        if env=='ruh':c.require(inv==approval['inspection']['targets']['mo-standard-2'],'RUH retained data changed after inspection')
        state['retained_inventory']=inv
        state['targets']['mo-standard-2']={'claim':f'cs-{env}-incremental-n2-v1','binding':target2,
            'batches':copy.deepcopy(inv['acknowledged_receipts']),'retained_acknowledged':inv['acknowledged']}
        s.journal();started=time.monotonic()
        for number in inv['pending']:
            s.guard('mo-standard-2');left=copy_budget-(time.monotonic()-started);c.require(left>0,'remaining copy deadline')
            selected=inv['partial_selections'].get(str(number))
            batch=b.stream_transfer(s.command(execute(src,'export-batch',number=number,selected=selected)),
                s.command(execute(p2,'receive-batch',target2,number)),timeout=min(900,left),await_ack=True)
            c.require(batch['success'] and batch['number']==number and batch['batches']==inv['batches']
                and batch['compiler_invocations']==batch['external_downloads']==0,'resumed batch receipt differs')
            state['targets']['mo-standard-2']['batches'].append(batch);s.journal()
        state['targets']['mo-standard-2']['batches'].sort(key=lambda x:x['number'])
        c.require([x['number'] for x in state['targets']['mo-standard-2']['batches']]==list(range(inv['batches'])),'resumed batch coverage differs')
        received=s.call(execute(p2,'finish',target2),timeout=600);state['targets']['mo-standard-2']['received']=received;s.journal()
        verified=s.call(execute(p2,'verify',target2),timeout=600);c.require(verified==received and verified['success'],'node2 final independent hash differs')
        state['targets']['mo-standard-2']['verify']=verified
        state['post_copy_resources']={n:s.guard(n) for n in policy['node_uids']};s.journal()
        for row in state['created']:
            if row['kind']=='Pod':s.delete(row,'deleted')
        s.absent(state['deleted']);state.update(helper_pods_absent=True,complete=True,completed_at=b.pub.now().isoformat());s.journal();return state
    except Exception as error:
        if hasattr(error,'transfer_diagnostic'):state['transfer_diagnostic']=error.transfer_diagnostic
        state.update(error_type=type(error).__name__,failed_at=b.pub.now().isoformat(),
            failure_sites=[{'file':Path(x.filename).name,'line':x.lineno} for x in traceback.extract_tb(error.__traceback__) if Path(x.filename).name.startswith('node_cache_')])
        s.journal();raise


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for k in ('policy','catalog','lock','approval','kubeconfig','directory'):p.add_argument('--'+k,type=Path,required=True)
    a=p.parse_args()
    try:run(c.read_json(a.policy),c.read_json(a.catalog),c.read_json(a.lock),c.read_json(a.approval),a.kubeconfig,a.directory)
    except Exception as error:
        print(json.dumps({'complete':False,'error_type':type(error).__name__,'automatic_retry':False}));raise SystemExit(1)
