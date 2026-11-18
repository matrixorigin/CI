#!/usr/bin/env python3
"""Journalled replication to two NEW per-node PVCs, without any compiler calls.

Only create ConfigMap/PVC/small helper Pods and UID-delete those helper Pods.
No old cache writes, pool changes, test dispatch or automatic mutation retries.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import incremental_cache as c
import node_cache_publish_control as pub
import node_cache_replication as replication
import node_compile_cache as n
import node_cache_toolchain_store as tools

HERE = Path(__file__).parent
FILES = ('incremental_cache.py','cs_coverage_cache.py','node_compile_cache.py',
         'node_cache_toolchain.py','node_cache_toolchain_store.py','node_cache_replication.py')


def snapshot_guard(policy, snap, node_name, owned=(), baseline=None, allocate=False):
    env=policy['environment']
    c.require(env in ('mty','jed','ruh','mnl2') and node_name in policy['node_uids'], 'replication target not approved')
    namespaces={x['metadata']['name']:x['metadata']['uid'] for x in snap['namespaces']['items']}
    c.require(all(namespaces.get(k)==v for k,v in policy['namespace_uids'].items()), 'replication namespace identity')
    c.require(not ({'mocservices-'+e for e in ('mty','jed','ruh','mnl2','jhb','pom')} - {'mocservices-'+env}) & namespaces.keys(), 'foreign environment')
    nodes={x['metadata']['name']:x for x in snap['nodes']['items']}
    for name,uid in policy['node_uids'].items():
        node=nodes[name];conditions={x['type']:x['status'] for x in node['status']['conditions']}
        c.require(node['metadata']['uid']==uid and not node['spec'].get('unschedulable') and conditions.get('Ready')=='True'
                  and all(conditions.get(k)=='False' for k in ('MemoryPressure','DiskPressure','PIDPressure')), 'replication node identity or pressure')
    sc=snap['sc'];c.require(sc['provisioner']=='directpv-min-io' and sc['volumeBindingMode']=='WaitForFirstConsumer'
                           and sc['parameters']['directpv.min.io/tier']=='ephemeral', 'replication storage class')
    stat=snap['stats']['node'];now=pub.now()
    for key in ('cpu','memory','fs'):
        checked=dt.datetime.fromisoformat(stat[key]['time'].replace('Z','+00:00'))
        c.require(0 <= (now-checked).total_seconds() <= 120, 'replication stale metrics')
    # This is a bounded COPY Pod, not a 7c/14Gi UT runner. UT gates are unchanged.
    c.require(stat['cpu']['usageNanoCores']<6e9 and stat['memory']['availableBytes']>=4*c.GIB
              and stat['fs']['availableBytes']>=60*c.GIB, 'replication actual reserve')
    c.require(any(d['metadata'].get('labels',{}).get('directpv.min.io/node')==node_name
                  and d['metadata']['labels'].get('directpv.min.io/tier')=='ephemeral'
                  and d['status'].get('status')=='Ready' and d['status'].get('freeCapacity',0)>=(128 if allocate else 64)*c.GIB
                  for d in snap['drives']['items']), 'replication physical reserve')
    cpu,memory=1.,512*1024**2;workloads={}
    for pod in snap['pods']['items']:
        if pod.get('status',{}).get('phase') in ('Succeeded','Failed'):continue
        c.require(not any(x['kind']=='EphemeralRunner' for x in pod['metadata'].get('ownerReferences',[])), 'active CI runner; wait without cancelling')
        if pod['metadata']['uid'] in owned or pod['spec'].get('nodeName')!=node_name:continue
        status=pod['status'];c.require(status['phase']=='Running' and any(x['type']=='Ready' and x['status']=='True'
                         for x in status.get('conditions',[])), 'existing workload unready')
        row={**pub.workload(pod),'uid':pod['metadata']['uid'],
             'init_restarts':sum(x.get('restartCount',0) for x in status.get('initContainerStatuses',[]))}
        workloads[pod['metadata']['namespace']+'/'+pod['metadata']['name']]=row
        a,b=pub.pod_budget(pod);cpu+=a;memory+=b
    c.require(baseline is None or workloads==baseline, 'workload changed during replication')
    alloc=nodes[node_name]['status']['allocatable']
    c.require(cpu+2<=pub.quantity(alloc['cpu']) and memory+4*c.GIB<=pub.quantity(alloc['memory']), 'replication declared reserve')
    return {'checked_at':now.isoformat(),'node':node_name,'workloads':workloads,
            'root_free_bytes':stat['fs']['availableBytes'],'memory_available_bytes':stat['memory']['availableBytes']}


def helper(name,node,claim,cm,readonly):
    resource = {'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':'cicd',
        'labels':{'codex.matrixorigin.io/replication':cm}},'spec':{
        'automountServiceAccountToken':False,'restartPolicy':'Never','activeDeadlineSeconds':4500,
        'nodeSelector':{'kubernetes.io/hostname':node,'matrixone.cloud/profile':'cn.cloudsigma'},
        'tolerations':[{'key':'profile.matrixone.cloud/cn.cloudsigma','value':'true','effect':'NoSchedule'}],
        'securityContext':{'runAsUser':1001,'runAsGroup':1001,'fsGroup':1001},
        'containers':[{'name':'copy','image':pub.IMAGE,'command':['/bin/bash','-euc','python3 --version; sleep 4400'],
            'resources':{'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'1','memory':'512Mi'}},
            'securityContext':{'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}},
            'volumeMounts':[{'name':'cache','mountPath':'/ci-cache','readOnly':readonly},
                            {'name':'script','mountPath':'/probe','readOnly':True}]}],
        'volumes':[{'name':'cache','persistentVolumeClaim':{'claimName':claim,'readOnly':readonly}},
                   {'name':'script','configMap':{'name':cm}}]}}
    if not readonly:
        resource['spec']['containers'][0]['volumeMounts'][0].pop('readOnly')
        resource['spec']['volumes'][0]['persistentVolumeClaim'].pop('readOnly')
    return resource


def subset(want,got):
    if isinstance(want,dict):return isinstance(got,dict) and all(k in got and subset(v,got[k]) for k,v in want.items())
    if isinstance(want,list):return isinstance(got,list) and len(want)==len(got) and all(subset(a,b) for a,b in zip(want,got))
    return want==got


def run(policy,catalog,lock,config,directory,api=n.legacy.api):
    c.require(not directory.exists(), 'replication intent exists; reconcile without retry')
    directory.mkdir();report=directory/'state.json'
    env=policy['environment'];cm=f'cs-{env}-cache-replication-v1'
    source=next(x for x in catalog['nodes'] if x['environment']==env and x['node']=='mo-standard-0')
    state={'environment':env,'complete':False,'compiler_invocations':0,'created':[],'deleted':[],
           'started_at':pub.now().isoformat(),'targets':{},'automatic_retry':False}
    def journal():pub.write_journal(report,state)
    def cmd(args):return ['kubectl','--kubeconfig',str(config),'--request-timeout=30s',*args]
    def call(args,data=None):
        proc=subprocess.run(cmd(args),input=None if data is None else c.canonical(data).decode(),capture_output=True,text=True,timeout=45)
        c.require(proc.returncode==0,'scoped replication API operation failed')
        return json.loads(proc.stdout) if proc.stdout.strip() else None
    def snap(node):return {key:call(args) for key,args in {
        'namespaces':['get','namespace','-o','json'],'nodes':['get','nodes','-o','json'],
        'pods':['get','pods','-A','-o','json'],'sc':['get','sc','directpv-min-io-ephemeral','-o','json'],
        'drives':['get','directpvdrives','-o','json'],'stats':['get','--raw','/api/v1/nodes/'+node+'/proxy/stats/summary']}.items()}
    def create(resource):
        kind,name=resource['kind'],resource['metadata']['name']
        c.require(call(['get',kind,name,'-n','cicd','--ignore-not-found','-o','json']) is None,'resource exists; reconcile without retry')
        state['pending_create']={'kind':kind,'name':name};journal()
        made=call(['create','-f','-','-o','json'],resource)
        state['created'].append({'kind':kind,'name':name,'uid':made['metadata']['uid']})
        state.pop('pending_create');journal();return made
    def wait_pod(name,node,claim,readonly):
        deadline=time.monotonic()+240
        while time.monotonic()<deadline:
            pod=call(['get','pod',name,'-n','cicd','-o','json'])
            row=next(x for x in state['created'] if x['kind']=='Pod' and x['name']==name)
            c.require(pod['metadata']['uid']==row['uid'] and not pod['metadata'].get('deletionTimestamp'),'helper identity changed')
            if pod['status'].get('phase')=='Running' and any(x['type']=='Ready' and x['status']=='True' for x in pod['status'].get('conditions',[])):
                c.require(pod['spec']['nodeName']==node and pod['spec'].get('automountServiceAccountToken') is False,'helper node differs')
                c.require(not pod['metadata'].get('ownerReferences') and subset(helper(name,node,claim,cm,readonly)['spec'],pod['spec']), 'helper template or readonly mount differs')
                return pod
            c.require(pod['status'].get('phase') not in ('Failed','Succeeded'),'helper exited')
            time.sleep(3)
        raise TimeoutError('helper readiness; no automatic retry')
    def exec_args(pod,mode,target=None):
        args=['exec',pod,'-n','cicd']
        if mode=='receive':args+=['-i']
        args+=['--','python3','/probe/node_cache_replication.py',mode,'--catalog','/probe/catalog.json',
               '--lock','/probe/lock.json','--source','/probe/source.json']
        if target is not None:args+=['--target-json',c.canonical(target).decode()]
        return args
    try:
        journal();entries=replication.entries_for(catalog,source)
        for entry in entries:
            c.require(n.reviewed_entries({'entries':[entry]},entry['runtime'],api)==[entry],'reviewed producer unavailable')
        tools.authorize(lock,api(f'repos/{lock["producer"]["repository"]}/actions/runs/{lock["producer"]["id"]}'),
                        api(f'repos/{lock["producer"]["repository"]}/actions/artifacts/{lock["artifact"]["id"]}'))
        state['provenance_verified_before_write']=True
        baselines={}
        for node in policy['node_uids']:
            baselines[node]=snapshot_guard(policy,snap(node),node,allocate=node!='mo-standard-0')
        source_pvc=call(['get','pvc',policy['name'],'-n','cicd','-o','json'])
        source_pv=call(['get','pv',source_pvc['spec']['volumeName'],'-o','json'])
        c.require(source_pvc['metadata']['uid']==source['pvc_uid'] and source_pv['metadata']['uid']==source['pv_uid']
                  and source_pvc['status']['phase']=='Bound' and source_pv['spec']['claimRef']['uid']==source['pvc_uid'], 'source volume replaced')
        data={name:(HERE/name).read_text() for name in FILES}
        data.update({'catalog.json':c.canonical(catalog).decode(),'lock.json':c.canonical(lock).decode(),
                     'source.json':c.canonical(source).decode()})
        c.require(sum(len(k.encode())+len(v.encode()) for k,v in data.items())<900000,'replication ConfigMap budget')
        snapshot_guard(policy,snap('mo-standard-0'),'mo-standard-0',baseline=baselines['mo-standard-0']['workloads'])
        create({'apiVersion':'v1','kind':'ConfigMap','immutable':True,'metadata':{'name':cm,'namespace':'cicd'},'data':data})
        source_pod=cm+'-source';create(helper(source_pod,'mo-standard-0',policy['name'],cm,True));wait_pod(source_pod,'mo-standard-0',policy['name'],True)
        plan=call(exec_args(source_pod,'plan'));payload=replication.expected_files(plan,catalog,source,lock)
        bytes_needed=sum(x[0]+1024 for x in payload.values())+len(c.canonical(plan))+10240
        c.require(shutil.disk_usage(directory).free>=bytes_needed+20*c.GIB,'controller transfer disk reserve')
        bundle=directory/'reviewed-cache.tar';state['export_started_at']=pub.now().isoformat();journal()
        with bundle.open('xb') as out:
            proc=subprocess.run(cmd(exec_args(source_pod,'export')),stdout=out,stderr=subprocess.PIPE,timeout=3700)
        c.require(proc.returncode==0 and bundle.stat().st_size<=bytes_needed,'source export failed or oversized')
        state['export']={'bytes':bundle.stat().st_size,'sha256':c.digest(bundle),'completed_at':pub.now().isoformat()};journal()
        for number in (1,2):
            node=f'mo-standard-{number}';claim=f'cs-{env}-incremental-n{number}-v1';pod=cm+f'-n{number}'
            owned=[x['uid'] for x in state['created'] if x['kind']=='Pod']
            snapshot_guard(policy,snap(node),node,owned,baselines[node]['workloads'],True)
            made=create({'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':claim,'namespace':'cicd'},
                 'spec':{'accessModes':['ReadWriteOnce'],'volumeMode':'Filesystem','storageClassName':'directpv-min-io-ephemeral',
                         'resources':{'requests':{'storage':'64Gi'}}}})
            create(helper(pod,node,claim,cm,False));wait_pod(pod,node,claim,False)
            pvc=call(['get','pvc',claim,'-n','cicd','-o','json']);pv=call(['get','pv',pvc['spec']['volumeName'],'-o','json'])
            c.require(pvc['metadata']['uid']==made['metadata']['uid'] and pvc['status']['phase']=='Bound'
                      and pv['status']['phase']=='Bound' and pv['spec']['claimRef']['uid']==pvc['metadata']['uid']
                      and pv['spec']['claimRef']['name']==claim and pv['spec']['claimRef']['namespace']=='cicd'
                      and not pvc['metadata'].get('ownerReferences') and not pv['metadata'].get('ownerReferences')
                      and not pvc['metadata'].get('deletionTimestamp') and not pv['metadata'].get('deletionTimestamp')
                      and pvc['spec']['accessModes']==pv['spec']['accessModes']==['ReadWriteOnce']
                      and pvc['spec']['storageClassName']==pv['spec']['storageClassName']=='directpv-min-io-ephemeral'
                      and pub.quantity(pvc['status']['capacity']['storage'])==64*c.GIB
                      and pv['spec']['csi']['driver']=='directpv-min-io','target volume binding differs')
            terms=pv['spec']['nodeAffinity']['required']['nodeSelectorTerms']
            c.require(len(terms)==1 and any(x=={'key':'directpv.min.io/node','operator':'In','values':[node]}
                     for x in terms[0]['matchExpressions']),'target PV topology differs')
            target={'environment':env,'node':node,'node_uid':policy['node_uids'][node],
                    'pvc_uid':pvc['metadata']['uid'],'pv_uid':pv['metadata']['uid']}
            state['targets'][node]={'claim':claim,'binding':target,'started_at':pub.now().isoformat()};journal()
            owned=[x['uid'] for x in state['created'] if x['kind']=='Pod']
            snapshot_guard(policy,snap(node),node,owned,baselines[node]['workloads'])
            with bundle.open('rb') as incoming:
                proc=subprocess.run(cmd(exec_args(pod,'receive',target)),stdin=incoming,capture_output=True,text=True,timeout=3700)
            c.require(proc.returncode==0,'target transfer stopped; preserve partial data without retry')
            state['targets'][node]['received']=json.loads(proc.stdout);journal()
            result=call(exec_args(pod,'verify',target));c.require(result['success'] and result['compiler_invocations']==0,'independent target verification failed')
            state['targets'][node]['verify']=result;state['targets'][node]['completed_at']=pub.now().isoformat();journal()
        # Delete only exact helper UIDs. PVC/cache/ConfigMap/transfer evidence retained.
        for row in state['created']:
            if row['kind']!='Pod':continue
            current=call(['get','pod',row['name'],'-n','cicd','-o','json'])
            c.require(current['metadata']['uid']==row['uid'],'cleanup helper replaced')
            call(['delete','--raw','/api/v1/namespaces/cicd/pods/'+row['name'],'-f','-'],
                 {'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':row['uid']},'gracePeriodSeconds':1})
            state['deleted'].append(row);journal()
        deadline=time.monotonic()+120
        remaining=True
        while remaining and time.monotonic()<deadline:
            remaining=any(call(['get','pod',row['name'],'-n','cicd','--ignore-not-found','-o','json']) is not None
                          for row in state['deleted'])
            if remaining:time.sleep(2)
        c.require(not remaining,'helper deletion not yet confirmed; do not dispatch tests')
        state['helper_pods_absent']=True
        state.update(complete=True,completed_at=pub.now().isoformat());journal();return state
    except Exception as error:
        state.update(error_type=type(error).__name__,failed_at=pub.now().isoformat());journal();raise


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('policy','catalog','lock','kubeconfig','directory'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    try:
        result=run(c.read_json(args.policy),c.read_json(args.catalog),c.read_json(args.lock),args.kubeconfig,args.directory)
        print(json.dumps({'complete':result['complete'],'environment':result['environment'],'compiler_invocations':0}))
    except Exception as error:
        print(json.dumps({'complete':False,'error_type':type(error).__name__,'automatic_retry':False}));raise SystemExit(1)
