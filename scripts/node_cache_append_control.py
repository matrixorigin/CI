#!/usr/bin/env python3
"""Protected append controller: new CM/Pod only, exact existing owned PVC.

No runner changes, PVC creation, cache deletion or automatic retry. Each
successful candidate needs independent review and one unique operation name.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

import incremental_cache as c
import node_cache_publish_control as initial
import node_cache_fleet_control as fleet
import node_cache_publish_job as authorization
import node_cache_transport as transport


def objects(policy,entry,base,name):
    c.require(re.fullmatch('cs-'+policy['environment']+r'-nc-append-[1-9][0-9]*-[1-9][0-9]*',name),
              'append operation name invalid')
    c.require(name==f"cs-{policy['environment']}-nc-append-{entry['producer']['id']}-{entry['artifact']['id']}",
              'append operation not bound to producer artifact')
    cm,_,pod=copy.deepcopy(initial.objects(policy,entry))
    for resource in (cm,pod):
        resource['metadata']['name']=name
        resource['metadata']['labels']={'codex.matrixorigin.io/experiment':name}
    for filename in ('node_cache_append.py','node_cache_append_job.py'):
        cm['data'][filename]=(initial.HERE/filename).read_text()
    cm['data']['append-base.json']=c.canonical(base).decode()
    c.require(sum(len(k.encode())+len(v.encode()) for k,v in cm['data'].items())<900000,'append ConfigMap size')
    pod['spec']['volumes'][-1]['configMap']['name']=name
    return [cm,pod]


def identity(policy,receipt,base,resources,created,cm,pod,pvc,pv,node):
    binding=fleet.volume(policy,receipt,pvc,pv,node)
    c.require({k:v for k,v in binding.items() if k!='marker_sha256'}==base['binding'] and
              binding['marker_sha256']==base['marker_sha256'],'append published volume identity changed')
    uids={x['kind']:x['uid'] for x in created};name=resources[0]['metadata']['name']
    for kind,value in (('ConfigMap',cm),('Pod',pod)):
        c.require(value['metadata']['name']==name and value['metadata']['namespace']=='cicd' and
                  value['metadata']['uid']==uids[kind] and not value['metadata'].get('ownerReferences') and
                  not value['metadata'].get('deletionTimestamp'),'append resource identity changed')
    c.require(cm.get('immutable') is True and cm['data']==resources[0]['data'],'append approval mutated')
    expected=resources[1]['spec'];spec=pod['spec']
    c.require(spec['nodeName']==policy['node'] and pod['status']['phase']=='Running' and
              any(x['type']=='Ready' and x['status']=='True' for x in pod['status']['conditions']),
              'append Pod not ready')
    for key in ('automountServiceAccountToken','restartPolicy','activeDeadlineSeconds','nodeSelector','tolerations','securityContext'):
        c.require(spec[key]==expected[key],'append Pod security/scheduling differs')
    volumes=copy.deepcopy(spec['volumes'])
    for volume in volumes:
        if 'configMap' in volume:
            c.require(volume['configMap'].get('defaultMode',420)==420,'append ConfigMap mode differs')
            volume['configMap'].pop('defaultMode',None)
    c.require(volumes==expected['volumes'] and len(spec['containers'])==1 and
              not spec.get('initContainers') and not spec.get('ephemeralContainers'),'append volume/container scope differs')
    for key,value in expected['containers'][0].items():
        c.require(spec['containers'][0][key]==value,'append container differs')
    return base['binding']


def run(policy,entry,base,receipt,config,path):
    c.require(not path.exists() and entry['runtime']['environment']==policy['environment'] and
              not entry['bindings'],'fresh append operation required')
    name=f"cs-{policy['environment']}-nc-append-{entry['producer']['id']}-{entry['artifact']['id']}"
    resources=objects(policy,entry,base,name)
    result={'complete':False,'environment':policy['environment'],'name':name,'created':[],
            'started_at':initial.now().isoformat(),'compiler_invocations':0,'existing_pvc_preserved':True}
    def journal():initial.write_journal(path,result)
    def call(args,data=None,timeout=60):
        proc=subprocess.run(['kubectl','--kubeconfig',str(config),'--request-timeout=30s',*args],
            input=None if data is None else c.canonical(data).decode(),capture_output=True,text=True,timeout=timeout)
        c.require(proc.returncode==0,'append scoped Kubernetes request incomplete')
        return json.loads(proc.stdout) if proc.stdout.strip() else None
    def snapshot():
        return {key:call(args) for key,args in {
            'namespaces':['get','namespace','-o','json'],'nodes':['get','nodes','-o','json'],
            'sc':['get','sc','directpv-min-io-ephemeral','-o','json'],
            'drives':['get','directpvdrives','-o','json'],'pods':['get','pods','-A','-o','json'],
            'stats':['get','--raw','/api/v1/nodes/'+policy['node']+'/proxy/stats/summary']}.items()}
    def fresh_authorization():
        token=os.environ['CS_CACHE_READ_TOKEN'];prefix='repos/matrixorigin/matrixone/actions/'
        def api(suffix):
            req=urllib.request.Request('https://api.github.com/'+prefix+suffix,
                headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json'})
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self,*args,**kwargs):return None
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=30) as body:return json.load(body)
        auth={'run':api('runs/'+str(entry['producer']['id'])),
              'artifact':api('artifacts/'+str(entry['artifact']['id'])),'checked_at':initial.now().isoformat()}
        authorization.authorize(entry,auth)
        return auth
    def volume():
        pvc=call(['get','pvc',policy['name'],'-n','cicd','-o','json'])
        pv=call(['get','pv',pvc['spec']['volumeName'],'-o','json'])
        node=call(['get','node',policy['node'],'-o','json'])
        binding=fleet.volume(policy,receipt,pvc,pv,node)
        c.require(binding=={**base['binding'],'marker_sha256':base['marker_sha256']},'append base binding differs')
        return pvc,pv,node
    def current_identity():
        pvc,pv,node=volume()
        return identity(policy,receipt,base,resources,result['created'],
            call(['get','configmap',name,'-n','cicd','-o','json']),
            call(['get','pod',name,'-n','cicd','-o','json']),pvc,pv,node)
    try:
        fresh_authorization();authorization.validate_url(transport.signed_archive_url(entry))
        snap=snapshot();result['preflight']=initial.guard(policy,snap,after_allocation=True)
        c.require(result['preflight']['root_free_bytes']>=60*c.GIB,'append startup disk reserve')
        policy={**policy,'initial_init_restarts':result['preflight']['init_restarts']}
        volume();journal()
        for obj in resources:
            c.require(call(['get',obj['kind'],name,'-n','cicd','--ignore-not-found','-o','json']) is None,
                      'append resource exists; reconcile without retry')
        initial.guard(policy,snap,after_allocation=True)
        for obj in resources:
            result['pending_create']={'kind':obj['kind'],'name':name};journal()
            live=call(['create','-f','-','-o','json'],obj)
            result['created'].append({'kind':obj['kind'],'name':name,'uid':live['metadata']['uid']});journal()
        result.pop('pending_create');journal()
        waited=subprocess.run(['kubectl','--kubeconfig',str(config),'-n','cicd','wait',
            '--for=condition=Ready','pod/'+name,'--timeout=180s'],capture_output=True,text=True,timeout=200)
        c.require(waited.returncode==0,'append Pod scheduling incomplete')
        uid=next(x['uid'] for x in result['created'] if x['kind']=='Pod')
        result['binding']=current_identity();journal()
        for phase in ('stage','append','verify'):
            result['latest_resources']=initial.guard(policy,snapshot(),uid,True)
            c.require(current_identity()==result['binding'],'append binding changed')
            request={**fresh_authorization(),'binding':result['binding']}
            if phase=='stage':request['url']=transport.signed_archive_url(entry)
            result['active_phase']=phase;journal();start=time.monotonic()
            response=call(['exec','-i','-n','cicd',name,'--','python3','/probe/node_cache_append_job.py',phase],request,3700)
            c.require(response['success'] is True and response['phase']==phase,'append phase rejected')
            result[phase]={**response,'controller_seconds':round(time.monotonic()-start,3)};journal()
        c.require(current_identity()==result['binding'],'append identity changed before cleanup')
        result['pending_delete']={'kind':'Pod','name':name,'uid':uid};journal()
        call(['delete','--raw','/api/v1/namespaces/cicd/pods/'+name,'-f','-'],
             {'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':uid},'gracePeriodSeconds':30})
        for _ in range(30):
            if call(['get','pod',name,'-n','cicd','--ignore-not-found','-o','json']) is None:break
            time.sleep(2)
        else:raise ValueError('append own Pod deletion unconfirmed')
        result['deleted']=[result.pop('pending_delete')]
        result.update(complete=True,completed_at=initial.now().isoformat());result.pop('active_phase');journal()
        return result
    except Exception as error:
        result.update(diagnostic=c.failure_report(error),automatic_retry=False);journal();raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('policy','entry','base','receipt','kubeconfig','report'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    try:run(c.read_json(args.policy),c.read_json(args.entry),c.read_json(args.base),
            c.read_json(args.receipt),args.kubeconfig,args.report)
    except Exception as error:
        print(c.canonical({'complete':False,'diagnostic':c.failure_report(error),'automatic_retry':False}).decode())
        sys.exit(1)
