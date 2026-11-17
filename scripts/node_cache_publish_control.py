#!/usr/bin/env python3
"""Initial node-cache publication, ONLY new PVC/ConfigMap/Pod resources.

Protected orchestration supplies a reviewed policy, not test-controlled input.
No apply/update, runner registration, deployment or automatic retry. Ambiguous
create/exec results stop with a journal; existing data is never overwritten.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

import incremental_cache as c
import node_compile_cache as n
import node_cache_transport as transport
import node_cache_publish_job as job

HERE=Path(__file__).parent
IMAGE='ghcr.io/actions/actions-runner@sha256:6d0b2d80d80f143f77dcb676e50920100bbb547e39e883454d57f45ab8c31300'
FILES=('incremental_cache.py','cs_coverage_cache.py','node_compile_cache.py',
       'node_cache_transport.py','node_cache_bundle.py','publish_node_cache.py','node_cache_publish_job.py')


def now():return dt.datetime.now(dt.timezone.utc)


def quantity(value):
    for suffix,factor in [('Ki',1024),('Mi',2**20),('Gi',c.GIB),('m',.001)]:
        if str(value).endswith(suffix):return float(str(value)[:-len(suffix)])*factor
    return float(value)


def pod_budget(pod):
    def resources(containers):
        return [sum(max(quantity(x.get('resources',{}).get('limits',{}).get(k,0)),
                        quantity(x.get('resources',{}).get('requests',{}).get(k,0))) for x in containers)
                for k in ('cpu','memory')]
    spec=pod['spec'];budget=resources(spec['containers'])
    # Sidecar-style init containers persist; conservatively sum all init limits.
    init=resources(spec.get('initContainers',[]))
    return [a+b+quantity(spec.get('overhead',{}).get(k,0))
            for a,b,k in zip(budget,init,('cpu','memory'))]


def workload(pod):
    return {'namespace':pod['metadata']['namespace'],'name':pod['metadata']['name'],
            'owners':[x['kind'] for x in pod['metadata'].get('ownerReferences',[])],
            # Inventory policies historically count regular containers. Keep
            # the same meaning; init restart history is independently frozen
            # at the first fresh pre-write observation and checked thereafter.
            'restarts':sum(x.get('restartCount',0) for x in pod.get('status',{}).get('containerStatuses',[]))}


def guard(policy,snapshot,own_uid=None,after_allocation=False,at=None):
    at=at or now();environment=policy['environment'];target=policy['node']
    c.require(environment in ('mty','jed','ruh','mnl2') and target=='mo-standard-0', 'initial target not approved')
    namespaces={x['metadata']['name']:x['metadata']['uid'] for x in snapshot['namespaces']['items']}
    c.require(all(namespaces.get(k)==v for k,v in policy['namespace_uids'].items()),'namespace identity changed')
    c.require(not ({'mocservices-'+e for e in ('mty','jed','ruh','mnl2','jhb','pom')} -
                   {'mocservices-'+environment}) & namespaces.keys(),'foreign environment')
    nodes={x['metadata']['name']:x for x in snapshot['nodes']['items']}
    for name,uid in policy['node_uids'].items():
        node=nodes[name];conditions={x['type']:x['status'] for x in node['status']['conditions']}
        c.require(node['metadata']['uid']==uid and not node['spec'].get('unschedulable') and
                  conditions.get('Ready')=='True' and all(conditions.get(k)=='False'
                  for k in ('MemoryPressure','DiskPressure','PIDPressure')),'node identity or pressure')
    sc=snapshot['sc']
    c.require(sc.get('provisioner')=='directpv-min-io' and sc.get('volumeBindingMode')=='WaitForFirstConsumer'
              and sc['parameters'].get('directpv.min.io/tier')=='ephemeral','storage class differs')
    stat=snapshot['stats']['node']
    for key in ('cpu','memory','fs'):
        stamp=dt.datetime.fromisoformat(stat[key]['time'].replace('Z','+00:00'))
        c.require(0<=(at-stamp).total_seconds()<=120,'stale resource observation')
    c.require(stat['cpu']['usageNanoCores']<6e9 and stat['memory']['availableBytes']>=32*c.GIB and
              stat['fs']['availableBytes']>=(40 if after_allocation else 60)*c.GIB,'actual resource reserve')
    # Fresh physical capacity must leave 64 GiB after the new independent 64 GiB allocation.
    c.require(any(d['metadata'].get('labels',{}).get('directpv.min.io/node')==target and
                  d['metadata']['labels'].get('directpv.min.io/tier')=='ephemeral' and
                  d['status'].get('status')=='Ready' and
                  d['status'].get('freeCapacity',0)>=(64 if after_allocation else 128)*c.GIB
                  for d in snapshot['drives']['items']),'physical disk reserve')
    baseline={(x['namespace'],x['name']):x for x in policy['workloads']};seen=set();cpu=1.;memory=c.GIB;init_counts={}
    for pod in snapshot['pods']['items']:
        status=pod.get('status',{})
        if status.get('phase') in ('Succeeded','Failed'):continue
        # Any active CI runner, including unscheduled ones, is a conflict.
        owners=[x['kind'] for x in pod['metadata'].get('ownerReferences',[])]
        c.require('EphemeralRunner' not in owners,'active CI runner')
        if pod['spec'].get('nodeName')!=target:continue
        if own_uid and pod['metadata']['uid']==own_uid:continue
        observed=workload(pod);key=(observed['namespace'],observed['name'])
        c.require(key in baseline and observed==baseline[key] and status.get('phase')=='Running' and
                  any(x['type']=='Ready' and x['status']=='True' for x in status.get('conditions',[])),
                  'target workload changed, restarted or unready')
        init=status.get('initContainerStatuses',[])
        c.require(len(init)==len(pod['spec'].get('initContainers',[])) and
                  all(x.get('state',{}).get('terminated',{}).get('exitCode')==0 for x in init),
                  'init container incomplete or failed')
        init_counts['/'.join(key)]=sum(x.get('restartCount',0) for x in init)
        seen.add(key);a,b=pod_budget(pod);cpu+=a;memory+=b
    c.require(seen==set(baseline),'target workload disappeared')
    if 'initial_init_restarts' in policy:
        c.require(init_counts==policy['initial_init_restarts'],'init restart changed during publication')
    alloc=nodes[target]['status']['allocatable']
    c.require(cpu+2<=quantity(alloc['cpu']) and memory+4*c.GIB<=quantity(alloc['memory']),
              'declared resource reserve')
    return {'checked_at':at.isoformat(),'node':target,'node_uid':nodes[target]['metadata']['uid'],
            'cpu_cores':stat['cpu']['usageNanoCores']/1e9,'root_free_bytes':stat['fs']['availableBytes'],
            'memory_available_bytes':stat['memory']['availableBytes'],'init_restarts':init_counts,
            'existing_workloads_unchanged':True}


def objects(policy,entry):
    name=policy['name'];labels={'codex.matrixorigin.io/experiment':name}
    c.require(re.fullmatch(r'cs-'+policy['environment']+r'-incremental-n0-v[0-9]+',name), 'unapproved resource name')
    metadata={'name':name,'namespace':'cicd','labels':labels}
    data={f:(HERE/f).read_text() for f in FILES};data['reviewed-entry.json']=c.canonical(entry).decode()
    c.require(sum(len(k.encode())+len(v.encode()) for k,v in data.items())<900000,'ConfigMap size')
    return [
      {'apiVersion':'v1','kind':'ConfigMap','immutable':True,'metadata':metadata,'data':data},
      {'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':metadata,'spec':{
        'accessModes':['ReadWriteOnce'],'volumeMode':'Filesystem','storageClassName':'directpv-min-io-ephemeral',
        'resources':{'requests':{'storage':'64Gi'}}}},
      {'apiVersion':'v1','kind':'Pod','metadata':metadata,'spec':{
        'automountServiceAccountToken':False,'restartPolicy':'Never','activeDeadlineSeconds':4500,
        'nodeSelector':{'kubernetes.io/hostname':policy['node'],'matrixone.cloud/profile':'cn.cloudsigma'},
        'tolerations':[{'key':'profile.matrixone.cloud/cn.cloudsigma','value':'true','effect':'NoSchedule'}],
        'securityContext':{'runAsUser':1001,'runAsGroup':1001,'fsGroup':1001},
        'containers':[{'name':'publisher','image':IMAGE,'command':['/bin/bash','-euc','python3 --version; sleep 4400'],
          'env':[{'name':'CS_CACHE_ENV','value':policy['environment']},
                 {'name':'ACTUAL_NODE','valueFrom':{'fieldRef':{'fieldPath':'spec.nodeName'}}}],
          'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'1','memory':'1Gi'}},
          'securityContext':{'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}},
          'volumeMounts':[{'name':'cache','mountPath':'/ci-cache'},
                          {'name':'stage','mountPath':'/stage'},
                          {'name':'script','mountPath':'/probe','readOnly':True}]}],
        'volumes':[{'name':'cache','persistentVolumeClaim':{'claimName':name}},
                   {'name':'stage','emptyDir':{'sizeLimit':'16Gi'}},
                   {'name':'script','configMap':{'name':name}}]}}]


def bound_identity(policy,pod,pvc,pv,created,node):
    name=policy['name'];uids={x['kind']:x['uid'] for x in created}
    for kind,value in [('Pod',pod),('PersistentVolumeClaim',pvc)]:
        c.require(value['metadata']['name']==name and value['metadata']['namespace']=='cicd' and
                  value['metadata']['uid']==uids[kind] and not value['metadata'].get('deletionTimestamp') and
                  not value['metadata'].get('ownerReferences'),'created resource identity differs')
    c.require(pod['spec']['nodeName']==policy['node'] and pod['spec'].get('automountServiceAccountToken') is False
              and pod['status']['phase']=='Running' and any(x['type']=='Ready' and x['status']=='True'
              for x in pod['status']['conditions']),'publisher not ready or changed')
    expected=objects(policy,{'unused':True})[-1]['spec']
    import copy
    volumes=copy.deepcopy(pod['spec']['volumes'])
    for volume in volumes:
        if 'configMap' in volume:
            c.require(volume['configMap'].get('defaultMode',420)==420,'script mode changed')
            volume['configMap'].pop('defaultMode',None)
    c.require(volumes==expected['volumes'],'publisher volumes mutated')
    for key in ('nodeSelector','securityContext'):
        c.require(pod['spec'][key]==expected[key],'publisher scheduling mutated')
    c.require(pod['spec']['containers'][0]['image']==IMAGE and len(pod['spec']['containers'])==1 and
              not pod['spec'].get('initContainers'),'publisher container changed')
    for key in ('volumeMounts','command','resources','securityContext'):
        c.require(pod['spec']['containers'][0][key]==expected['containers'][0][key],'publisher container mutated')
    c.require(node['metadata']['uid']==policy['node_uids'][policy['node']], 'node UID changed')
    c.require(pvc['status']['phase']=='Bound' and pvc['spec']['storageClassName']=='directpv-min-io-ephemeral'
              and pvc['spec']['accessModes']==['ReadWriteOnce'] and pvc['spec'].get('volumeMode','Filesystem')=='Filesystem'
              and quantity(pvc['status']['capacity']['storage'])==64*c.GIB,'PVC differs')
    c.require(pvc['spec']['volumeName']==pv['metadata']['name'] and not pv['metadata'].get('deletionTimestamp')
              and not pv['metadata'].get('ownerReferences') and pv['spec']['claimRef']['uid']==uids['PersistentVolumeClaim']
              and pv['spec']['claimRef']['name']==name and pv['spec']['claimRef']['namespace']=='cicd'
              and pv['spec']['csi']['driver']=='directpv-min-io' and pv['spec']['accessModes']==['ReadWriteOnce']
              and pv['status']['phase']=='Bound','PV identity differs')
    terms=pv['spec']['nodeAffinity']['required']['nodeSelectorTerms']
    c.require(len(terms)==1 and not terms[0].get('matchFields'),'PV topology broad or unknown')
    expressions=terms[0]['matchExpressions'];labels=node['metadata']['labels']
    c.require(any(x=={'key':'directpv.min.io/node','operator':'In','values':[policy['node']]}
                  for x in expressions),'PV not fixed to target')
    for expr in expressions:
        c.require(expr['key'].startswith('directpv.min.io/') and expr['operator']=='In' and
                  expr['values']==[labels.get(expr['key'])],'PV topology does not match node')
    return {'environment':policy['environment'],'node':policy['node'],'node_uid':node['metadata']['uid'],
            'pvc_uid':pvc['metadata']['uid'],'pv_uid':pv['metadata']['uid']}


def run(policy,entry,config,report_path):
    c.require(entry['runtime']['environment']==policy['environment'] and not entry['bindings'],'approval scope')
    name=policy['name'];result={'complete':False,'environment':policy['environment'],'created':[],
                               'started_at':now().isoformat(),'compiler_invocations':0}
    def journal():c.atomic_json(report_path,result)
    def call(args,data=None,timeout=60):
        proc=subprocess.run(['kubectl','--kubeconfig',str(config),'--request-timeout=30s',*args],
                            input=None if data is None else c.canonical(data).decode(),capture_output=True,text=True,timeout=timeout)
        c.require(proc.returncode==0,'scoped Kubernetes request failed')
        return json.loads(proc.stdout) if proc.stdout.strip() else None
    def snapshot():
        return {key:call(args) for key,args in {
          'namespaces':['get','namespace','-o','json'],'nodes':['get','nodes','-o','json'],
          'sc':['get','sc','directpv-min-io-ephemeral','-o','json'],
          'drives':['get','directpvdrives','-o','json'],'pods':['get','pods','-A','-o','json'],
          'stats':['get','--raw','/api/v1/nodes/'+policy['node']+'/proxy/stats/summary']}.items()}
    def authorization():
        token=os.environ['CS_CACHE_READ_TOKEN'];base='repos/matrixorigin/matrixone/actions/'
        def api(path):
            c.require(path.startswith(base),'unapproved API path')
            req=urllib.request.Request('https://api.github.com/'+path,headers={
                'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json'})
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self,*args,**kwargs):return None
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=30) as body:
                return json.load(body)
        metadata={'run':api(base+'runs/'+str(entry['producer']['id'])),
                  'artifact':api(base+'artifacts/'+str(entry['artifact']['id'])),'checked_at':now().isoformat()}
        job.authorize(entry,metadata)
        return metadata
    try:
        authorization();job.validate_url(transport.signed_archive_url(entry))
        result['artifact_access_verified']=True
        snap=snapshot();result['preflight']=guard(policy,snap);journal()
        policy={**policy,'initial_init_restarts':result['preflight']['init_restarts']}
        resources=objects(policy,entry)
        for obj in resources:
            c.require(call(['get',obj['kind'],name,'-n','cicd','--ignore-not-found','-o','json']) is None,
                      'existing target; reconcile, never retry')
        # Recheck freshness immediately before the first write.
        guard(policy,snap)
        for obj in resources:
            result['pending_create']={'kind':obj['kind'],'name':name};journal()
            live=call(['create','-f','-','-o','json'],obj)
            result['created'].append({'kind':obj['kind'],'name':name,'uid':live['metadata']['uid']});journal()
        result.pop('pending_create');journal()
        proc=subprocess.run(['kubectl','--kubeconfig',str(config),'-n','cicd','wait','--for=condition=Ready',
                             'pod/'+name,'--timeout=180s'],capture_output=True,timeout=200)
        c.require(proc.returncode==0,'publisher scheduling incomplete')
        uid=next(x['uid'] for x in result['created'] if x['kind']=='Pod')
        def identity():
            pod=call(['get','pod',name,'-n','cicd','-o','json'])
            pvc=call(['get','pvc',name,'-n','cicd','-o','json'])
            pv=call(['get','pv',pvc['spec']['volumeName'],'-o','json'])
            node=call(['get','node',policy['node'],'-o','json'])
            return bound_identity(policy,pod,pvc,pv,result['created'],node)
        result['binding']=identity();journal()
        for phase in ('stage','publish','verify'):
            snap=snapshot();result['latest_resources']=guard(policy,snap,uid,True)
            binding=identity();c.require(binding==result['binding'],'binding changed')
            request={**authorization(),'binding':binding}
            if phase=='stage':request['url']=transport.signed_archive_url(entry)
            result['active_phase']=phase;journal();start=time.monotonic()
            receipt=call(['exec','-i','-n','cicd',name,'--','python3','/probe/node_cache_publish_job.py',phase],request,3700)
            c.require(receipt['success'] is True and receipt['phase']==phase,'publisher rejected phase')
            result[phase]={**receipt,'controller_seconds':round(time.monotonic()-start,3)};journal()
        c.require(identity()==result['binding'],'identity changed before cleanup')
        result['pending_delete']={'kind':'Pod','name':name,'uid':uid};journal()
        call(['delete','--raw','/api/v1/namespaces/cicd/pods/'+name,'-f','-'],
             {'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':uid},'gracePeriodSeconds':30})
        for _ in range(30):
            if call(['get','pod',name,'-n','cicd','--ignore-not-found','-o','json']) is None:break
            time.sleep(2)
        else:raise ValueError('own publisher deletion not confirmed')
        result['deleted']=[result.pop('pending_delete')]
        result.update(complete=True,completed_at=now().isoformat());result.pop('active_phase');journal()
        return result
    except Exception as error:
        import traceback
        result['error_type']=type(error).__name__
        result['frames']=[{'file':Path(f.filename).name,'line':f.lineno} for f in traceback.extract_tb(error.__traceback__)
                          if Path(f.filename).name.startswith('node_cache_')]
        result['automatic_retry']=False;journal()
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('policy','entry','kubeconfig','report'):parser.add_argument('--'+key,required=True,type=Path)
    args=parser.parse_args()
    try:run(c.read_json(args.policy),c.read_json(args.entry),args.kubeconfig,args.report)
    except Exception as error:
        print(json.dumps({'complete':False,'error_type':type(error).__name__,'automatic_retry':False}))
        sys.exit(1)
