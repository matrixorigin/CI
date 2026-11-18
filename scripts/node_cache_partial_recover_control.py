"""One explicitly approved JED partial recovery; never recreate existing data.

Only the two exact old copy helpers may be retired. Their PVC/CM are retained.
Fresh helpers complete node1's missing tool and copy the absent node2 cache.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time
import traceback

import incremental_cache as c
import node_cache_replicate_control as base
import node_cache_partial_replication as partial

ORIGINAL=44269608611
OLD_REV='ecc89fdfec0b7acd575fb9ef563f6f1d06b9fad6'
CM='cs-jed-cache-partial-r1'


def approved(approval,policy,lock):
    old=approval['receipt'];proof=approval['stage_reverification']
    c.require(policy['environment']=='jed' and approval['original_run_id']==ORIGINAL
        and old['environment']=='jed' and old['complete'] is False and old['error_type']=='ValueError'
        and old['failure_sites']==[{'file':'node_cache_replicate_control.py','line':242},
                                  {'file':'node_cache_replicate_control.py','line':116}]
        and old['deleted']==[] and 'pending_create' not in old
        and [r['kind'] for r in old['created']]==['ConfigMap','Pod','PersistentVolumeClaim','Pod']
        and set(old['targets'])=={'mo-standard-1'}
        and 'received' not in old['targets']['mo-standard-1'],'not the reviewed partial-copy failure')
    c.require(proof['original_run_id']==ORIGINAL and proof['environment']=='jed'
        and proof['readonly'] is True and proof['mutations']==0 and proof['bad']==[]
        and proof['verified_files']+1==proof['expected_files']==old['payload']['files']
        and proof['verified_bytes']+lock['archive_size']==old['payload']['bytes']
        and proof['missing']==[partial.tool_name(lock)] and proof['published'] is False
        and proof['compiler_invocations']==proof['external_downloads']==0,'partial proof differs')
    c.require(old['created'][0]['name']=='cs-jed-cache-replication-v3'
        and old['created'][1]['name']=='cs-jed-cache-replication-v3-source'
        and old['created'][2]['name']=='cs-jed-incremental-n1-v1'
        and old['created'][3]['name']=='cs-jed-cache-replication-v3-n1','old helper scope differs')
    return old,proof


def volume(policy,node,claim,pvc,pv,expected=None):
    c.require(pvc['metadata']['name']==claim and pvc['metadata']['namespace']=='cicd'
        and not pvc['metadata'].get('deletionTimestamp') and not pvc['metadata'].get('ownerReferences')
        and not pv['metadata'].get('deletionTimestamp') and not pv['metadata'].get('ownerReferences')
        and pvc['status']['phase']==pv['status']['phase']=='Bound'
        and pvc['spec']['volumeName']==pv['metadata']['name']
        and pv['spec']['claimRef']['name']==claim and pv['spec']['claimRef']['namespace']=='cicd'
        and pv['spec']['claimRef']['uid']==pvc['metadata']['uid']
        and pvc['spec']['accessModes']==pv['spec']['accessModes']==['ReadWriteOnce']
        and pvc['spec']['storageClassName']==pv['spec']['storageClassName']=='directpv-min-io-ephemeral'
        and pv['spec']['csi']['driver']=='directpv-min-io'
        and base.pub.quantity(pvc['status']['capacity']['storage'])==64*c.GIB,'partial recovery volume differs')
    terms=pv['spec']['nodeAffinity']['required']['nodeSelectorTerms']
    c.require(len(terms)==1 and not terms[0].get('matchFields')
        and any(x=={'key':'directpv.min.io/node','operator':'In','values':[node]}
                for x in terms[0]['matchExpressions']),'partial recovery topology differs')
    binding={'environment':'jed','node':node,'node_uid':policy['node_uids'][node],
        'pvc_uid':pvc['metadata']['uid'],'pv_uid':pv['metadata']['uid']}
    c.require(expected is None or binding==expected,'existing volume replaced')
    return binding


def run(policy,catalog,lock,approval,config,directory,api=base.n.legacy.api):
    c.require(not directory.exists(),'intent exists: reconcile without retry')
    old,proof=approved(approval,policy,lock)
    directory.mkdir();report=directory/'state.json'
    source=next(n for n in catalog['nodes'] if n['environment']=='jed' and n['node']=='mo-standard-0')
    state={'environment':'jed','complete':False,'compiler_invocations':0,'automatic_retry':False,
        'started_at':base.pub.now().isoformat(),'resumed_from':{'run_id':ORIGINAL,'started_at':old['started_at']},
        'adopted_resources':old['created'],'existing_compile_objects_not_recopied':True,
        'missing_tool_only_bytes':lock['archive_size'],'retired_helpers':[],'created':[],'deleted':[],'targets':{}}
    def journal():base.pub.write_journal(report,state)
    def command(args):return ['kubectl','--kubeconfig',str(config),'--request-timeout=30s',*args]
    def call(args,data=None,timeout=45):
        p=subprocess.run(command(args),input=None if data is None else c.canonical(data).decode(),capture_output=True,text=True,timeout=timeout)
        c.require(p.returncode==0,'scoped partial recovery operation failed')
        return json.loads(p.stdout) if p.stdout.strip() else None
    def snapshot(node):
        return {key:call(args) for key,args in {
            'namespaces':['get','namespace','-o','json'],'nodes':['get','nodes','-o','json'],
            'pods':['get','pods','-A','-o','json'],'sc':['get','sc','directpv-min-io-ephemeral','-o','json'],
            'drives':['get','directpvdrives','-o','json'],'stats':['get','--raw','/api/v1/nodes/'+node+'/proxy/stats/summary']}.items()}
    def owned():return [r['uid'] for r in old['created']+state['created'] if r['kind']=='Pod']
    def create(obj):
        kind,name=obj['kind'],obj['metadata']['name']
        c.require(call(['get',kind,name,'-n','cicd','--ignore-not-found','-o','json']) is None,'resource exists: reconcile')
        state['pending_create']={'kind':kind,'name':name};journal()
        made=call(['create','-f','-','-o','json'],obj)
        state['created'].append({'kind':kind,'name':name,'uid':made['metadata']['uid']})
        state.pop('pending_create');journal();return made
    def delete(row,key):
        pod=call(['get','pod',row['name'],'-n','cicd','-o','json'])
        c.require(pod['metadata']['uid']==row['uid'],'owned helper replaced')
        call(['delete','--raw','/api/v1/namespaces/cicd/pods/'+row['name'],'-f','-'],
             {'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':row['uid']},'gracePeriodSeconds':1})
        state[key].append(row);journal()
    def absent(rows):
        deadline=time.monotonic()+120
        while True:
            if all(call(['get','pod',r['name'],'-n','cicd','--ignore-not-found','-o','json']) is None for r in rows):return
            c.require(time.monotonic()<deadline,'owned helper deletion not confirmed');time.sleep(2)
    def guard(node):
        return base.snapshot_guard(policy,snapshot(node),node,owned(),baselines[node]['workloads'],False)
    def wait(name,node,claim,readonly):
        deadline=time.monotonic()+240
        row=next(r for r in state['created'] if r['kind']=='Pod' and r['name']==name)
        while True:
            pod=call(['get','pod',name,'-n','cicd','-o','json'])
            c.require(pod['metadata']['uid']==row['uid'] and base.checked_helper_spec(base.helper(name,node,claim,CM,readonly)['spec'],pod['spec']),
                      'new copy helper template differs')
            if pod.get('status',{}).get('phase')=='Running' and any(cn['type']=='Ready' and cn['status']=='True' for cn in pod['status'].get('conditions',[])):
                c.require(pod['spec']['nodeName']==node,'new helper node differs');return
            c.require(pod.get('status',{}).get('phase') not in ('Failed','Succeeded') and time.monotonic()<deadline,'copy helper not ready');time.sleep(2)
    def execute(pod,mode,target=None,module='node_cache_replication.py',number=None):
        args=['exec',pod,'-n','cicd','--','python3','/probe/'+module,mode,
              '--catalog','/probe/catalog.json','--lock','/probe/lock.json','--source','/probe/source.json']
        if target is not None:args+=['--target-json',json.dumps(target,sort_keys=True,separators=(',',':'))]
        if number is not None:args+=['--number',str(number)]
        return args
    def bound(node,claim,expected=None):
        pvc=call(['get','pvc',claim,'-n','cicd','-o','json']);pv=call(['get','pv',pvc['spec']['volumeName'],'-o','json'])
        return volume(policy,node,claim,pvc,pv,expected)
    try:
        journal();original=api('repos/matrixorigin/cloudsigma-gitops/actions/runs/'+str(ORIGINAL))
        c.require(original['head_sha']==OLD_REV and original['head_branch']=='codex/ruh-pvc-readiness'
            and original['event']=='workflow_dispatch' and original['conclusion']=='failure' and original['status']=='completed'
            and original['run_attempt']==1 and original['path']=='.github/workflows/cloudsigma-up.yaml'
            and original['actor']['login']=='Ariznawlll','original partial run provenance differs')
        for entry in base.replication.entries_for(catalog,source):
            c.require(base.n.reviewed_entries({'entries':[entry]},entry['runtime'],api)==[entry],'unreviewed producer')
        base.tools.authorize(lock,api(f'repos/{lock["producer"]["repository"]}/actions/runs/{lock["producer"]["id"]}'),
                             api(f'repos/{lock["producer"]["repository"]}/actions/artifacts/{lock["artifact"]["id"]}'))
        state['provenance_verified_before_write']=True
        baselines={node:base.snapshot_guard(policy,snapshot(node),node,owned(),allocate=node=='mo-standard-2') for node in policy['node_uids']}
        state['preflight']=baselines;journal()
        source_identity={k:source[k] for k in ('environment','node','node_uid','pvc_uid','pv_uid')}
        bound('mo-standard-0',policy['name'],source_identity)
        target1=bound('mo-standard-1','cs-jed-incremental-n1-v1',old['targets']['mo-standard-1']['binding'])
        c.require(call(['get','pvc','cs-jed-incremental-n2-v1','-n','cicd','--ignore-not-found','-o','json']) is None,'node2 already exists: reconcile')
        old_cm=call(['get','configmap',old['created'][0]['name'],'-n','cicd','-o','json'])
        c.require(old_cm['metadata']['uid']==old['created'][0]['uid'] and old_cm.get('immutable') is True,'old ConfigMap replaced')
        for row,node,claim,readonly in [(old['created'][1],'mo-standard-0',policy['name'],True),
                                        (old['created'][3],'mo-standard-1','cs-jed-incremental-n1-v1',False)]:
            pod=call(['get','pod',row['name'],'-n','cicd','-o','json'])
            c.require(pod['metadata']['uid']==row['uid'] and base.checked_helper_spec(base.helper(row['name'],node,claim,old['created'][0]['name'],readonly)['spec'],pod['spec']),
                      'old helper identity or template differs')
        # No producer data is touched. Only exact approved old helper Pods retire.
        for row in (old['created'][1],old['created'][3]):guard('mo-standard-0');delete(row,'retired_helpers')
        absent(state['retired_helpers'])
        data={name:(base.HERE/name).read_text() for name in base.FILES+('node_cache_partial_replication.py','node_cache_chunk_replication.py')}
        data.update({'catalog.json':c.canonical(catalog).decode(),'lock.json':c.canonical(lock).decode(),'source.json':c.canonical(source).decode()})
        c.require(sum(len(k.encode())+len(v.encode()) for k,v in data.items())<900000,'recovery ConfigMap budget')
        guard('mo-standard-0');create({'apiVersion':'v1','kind':'ConfigMap','immutable':True,'metadata':{'name':CM,'namespace':'cicd'},'data':data})
        src=CM+'-source';pod1=CM+'-n1'
        guard('mo-standard-0');create(base.helper(src,'mo-standard-0',policy['name'],CM,True));wait(src,'mo-standard-0',policy['name'],True)
        guard('mo-standard-1');create(base.helper(pod1,'mo-standard-1','cs-jed-incremental-n1-v1',CM,False));wait(pod1,'mo-standard-1','cs-jed-incremental-n1-v1',False)
        bound('mo-standard-1','cs-jed-incremental-n1-v1',target1)
        inspected=call(execute(pod1,'inspect',target1,'node_cache_partial_replication.py'),timeout=600)
        c.require(inspected['success'] and inspected['verified_files']==proof['verified_files'] and inspected['missing']==proof['missing'],'live partial stage changed')
        state['partial_reverified_before_write']=inspected;journal();guard('mo-standard-1')
        result=base.stream_transfer(command(execute(src,'export-tool',module='node_cache_partial_replication.py')),
            command(execute(pod1,'finish-tool',target1,'node_cache_partial_replication.py')))
        state['targets']['mo-standard-1']={'claim':'cs-jed-incremental-n1-v1','binding':target1,'received':result,'tool_only_recovery':True}
        journal();verified=call(execute(pod1,'verify',target1),timeout=600)
        c.require(verified==result and verified['success'],'partial independent verification differs')
        state['targets']['mo-standard-1']['verify']=verified;journal()
        node='mo-standard-2';claim='cs-jed-incremental-n2-v1';pod2=CM+'-n2'
        base.snapshot_guard(policy,snapshot(node),node,owned(),baselines[node]['workloads'],True)
        pvc=create({'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':claim,'namespace':'cicd'},'spec':{
            'accessModes':['ReadWriteOnce'],'volumeMode':'Filesystem','storageClassName':'directpv-min-io-ephemeral',
            'resources':{'requests':{'storage':'64Gi'}}}})
        guard(node);create(base.helper(pod2,node,claim,CM,False));wait(pod2,node,claim,False)
        target2=bound(node,claim);c.require(target2['pvc_uid']==pvc['metadata']['uid'],'new node2 claim replaced')
        state['targets'][node]={'claim':claim,'binding':target2};journal();guard(node)
        planned=call(execute(src,'plan-batches',module='node_cache_chunk_replication.py'),timeout=600)
        c.require(planned['bytes']==old['payload']['bytes'] and planned['files']==old['payload']['files']
            and 0<planned['batches']<=256,'node2 batch plan differs')
        state['targets'][node]['batches']=[];journal();copy_start=time.monotonic()
        for number in range(planned['batches']):
            # Preserve the original whole-node transfer deadline; bound every
            # connection separately and acknowledge only hash-verified batches.
            guard(node);left=3700-(time.monotonic()-copy_start);c.require(left>0,'whole-node transfer deadline')
            batch=base.stream_transfer(command(execute(src,'export-batch',module='node_cache_chunk_replication.py',number=number)),
                command(execute(pod2,'receive-batch',target2,'node_cache_chunk_replication.py',number)),timeout=min(900,left))
            c.require(batch['success'] is True and batch['number']==number and batch['batches']==planned['batches']
                and batch['compiler_invocations']==batch['external_downloads']==0,'node2 batch receipt differs')
            state['targets'][node]['batches'].append(batch);journal()
        c.require(sum(x['bytes'] for x in state['targets'][node]['batches'])==planned['bytes']
            and sum(x['files'] for x in state['targets'][node]['batches'])==planned['files'],'batch coverage differs')
        copied=call(execute(pod2,'finish',target2,'node_cache_chunk_replication.py'),timeout=600)
        state['targets'][node]['received']=copied;journal()
        verified=call(execute(pod2,'verify',target2),timeout=600)
        c.require(copied==verified and verified['success'],'node2 independent verification differs')
        state['targets'][node]['verify']=verified;journal()
        state['post_copy_resources']={node:guard(node) for node in policy['node_uids']};journal()
        for row in state['created']:
            if row['kind']=='Pod':delete(row,'deleted')
        absent(state['deleted']);state.update(helper_pods_absent=True,complete=True,completed_at=base.pub.now().isoformat());journal()
        return state
    except Exception as error:
        state.update(error_type=type(error).__name__,failed_at=base.pub.now().isoformat(),
            failure_sites=[{'file':Path(x.filename).name,'line':x.lineno} for x in traceback.extract_tb(error.__traceback__)
                           if Path(x.filename).name.startswith('node_cache_')]);journal();raise


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for key in ('policy','catalog','lock','approval','kubeconfig','directory'):parser.add_argument('--'+key,type=Path,required=True)
    a=parser.parse_args()
    try:run(c.read_json(a.policy),c.read_json(a.catalog),c.read_json(a.lock),c.read_json(a.approval),a.kubeconfig,a.directory)
    except Exception as error:
        print(json.dumps({'complete':False,'error_type':type(error).__name__,'automatic_retry':False}));raise SystemExit(1)
