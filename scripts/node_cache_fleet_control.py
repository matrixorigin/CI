#!/usr/bin/env python3
"""Create ONE new node-bound reader pool from an independently reviewed binding.

No existing pool changes, no shared volume writes, no test dispatch. The caller
must provide the immutable approved publication receipt and fresh policy.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import incremental_cache as c
import node_cache_publish_control as pub


def pool(policy,binding,original):
    env=policy['environment'];name=f'cs-{env}-nc-n0-v1';route=f'cs-{env}-node-cache-7c14g'
    c.require(original['metadata']['name']==f'cs-{env}-large-7c14g' and
              original['metadata']['labels']['app.kubernetes.io/version']=='0.14.1','original template differs')
    original_spec=original['spec'];secret=original_spec['githubConfigSecret']
    c.require(original_spec['githubConfigUrl']=='https://github.com/matrixorigin/matrixone' and
              isinstance(secret,str) and secret.startswith(original['metadata']['name']+'-'),'original auth reference differs')
    template=original_spec['template']['spec']
    c.require(len(template['containers'])==1 and template['containers'][0]['name']=='runner' and
              template['containers'][0]['image']==pub.IMAGE and not template.get('initContainers'),
              'requires reviewed plain runner image')
    resources={'requests':{'cpu':'7','memory':'12Gi'},'limits':{'cpu':'7','memory':'14Gi'}}
    c.require(template['containers'][0]['resources']==resources,'original resource template differs')
    c.require(binding['environment']==env and binding['node']==policy['node'] and
              binding['node_uid']==policy['node_uids'][policy['node']],'published binding differs')
    labels={'codex.matrixorigin.io/experiment':name,'app.kubernetes.io/name':name,
            'app.kubernetes.io/component':'autoscaling-runner-set','app.kubernetes.io/part-of':'gha-rs',
            'app.kubernetes.io/version':'0.14.1'}
    envs=[{'name':'ACTIONS_RUNNER_POD_NAME','valueFrom':{'fieldRef':{'fieldPath':'metadata.name'}}},
          {'name':'CS_CACHE_NODE','valueFrom':{'fieldRef':{'fieldPath':'spec.nodeName'}}}]
    for key,value in {'ENV':env,'EXPECTED_NODE':policy['node'],'NODE_UID':binding['node_uid'],
                      'PVC_UID':binding['pvc_uid'],'PV_UID':binding['pv_uid']}.items():
        envs.append({'name':'CS_CACHE_'+key,'value':value})
    return {'apiVersion':'actions.github.com/v1alpha1','kind':'AutoscalingRunnerSet',
      'metadata':{'name':name,'namespace':'cicd','labels':labels},'spec':{
        'githubConfigUrl':original_spec['githubConfigUrl'],'githubConfigSecret':secret,
        'runnerScaleSetName':name,'runnerScaleSetLabels':[route],
        'runnerGroup':original_spec.get('runnerGroup','Default'),'minRunners':0,'maxRunners':2,
        'listenerTemplate':copy.deepcopy(original_spec['listenerTemplate']),
        'template':{'metadata':{'labels':labels},'spec':{
          'automountServiceAccountToken':False,'serviceAccountName':'gha-runner','restartPolicy':'OnFailure',
          'nodeSelector':{'kubernetes.io/hostname':policy['node'],'matrixone.cloud/profile':'cn.cloudsigma'},
          'tolerations':[{'key':'profile.matrixone.cloud/cn.cloudsigma','value':'true','effect':'NoSchedule'}],
          'containers':[{'name':'runner','image':pub.IMAGE,'command':['/home/runner/run.sh'],
            'env':envs,'resources':resources,'volumeMounts':[
              {'name':'work','mountPath':'/home/runner/_work'},
              {'name':'cache','mountPath':'/ci-cache','readOnly':True}]}],
          'volumes':[{'name':'work','emptyDir':{}},
                     {'name':'cache','persistentVolumeClaim':{'claimName':policy['name'],'readOnly':True}}]}}}}


def guard(policy,snapshot):
    evidence=pub.guard(policy,snapshot,after_allocation=True)
    target=policy['node'];cpu=14.;memory=28*c.GIB
    c.require(evidence['root_free_bytes']>=60*c.GIB and evidence['memory_available_bytes']>=32*c.GIB,
              'two-reader actual reserve')
    for pod in snapshot['pods']['items']:
        if pod['spec'].get('nodeName')!=target or pod.get('status',{}).get('phase') in ('Succeeded','Failed'):continue
        a,b=pub.pod_budget(pod);cpu+=a;memory+=b
    node=next(n for n in snapshot['nodes']['items'] if n['metadata']['name']==target)
    c.require(cpu+2<=pub.quantity(node['status']['allocatable']['cpu']) and
              memory+4*c.GIB<=pub.quantity(node['status']['allocatable']['memory']),
              'two-reader declared limits/reserve')
    return {**evidence,'new_runner_limit':2,'combined_cpu_limits':cpu,'combined_memory_limits_bytes':memory}


def volume(policy,receipt,pvc,pv,node):
    c.require(receipt['complete'] is True and receipt['verify']['success'] is True and
              receipt['verify']['binding']==receipt['publish']['binding'],'publication not independently verified')
    binding=receipt['verify']['binding'];name=policy['name']
    c.require(node['metadata']['uid']==binding['node_uid']==policy['node_uids'][policy['node']], 'node replaced')
    c.require(pvc['metadata']['name']==name and pvc['metadata']['namespace']=='cicd' and
              pvc['metadata']['uid']==binding['pvc_uid'] and not pvc['metadata'].get('ownerReferences') and
              not pvc['metadata'].get('deletionTimestamp') and pvc['status']['phase']=='Bound' and
              pvc['spec']['storageClassName']=='directpv-min-io-ephemeral' and pvc['spec']['accessModes']==['ReadWriteOnce']
              and pvc['spec'].get('volumeMode','Filesystem')=='Filesystem','published PVC changed')
    c.require(pv['metadata']['name']==pvc['spec']['volumeName'] and pv['metadata']['uid']==binding['pv_uid'] and
              not pv['metadata'].get('deletionTimestamp') and not pv['metadata'].get('ownerReferences') and
              all(pv['spec']['claimRef'][k]==v for k,v in {'name':name,'namespace':'cicd','uid':binding['pvc_uid']}.items())
              and pv['spec']['csi']['driver']=='directpv-min-io' and pv['status']['phase']=='Bound'
              and pv['spec']['storageClassName']=='directpv-min-io-ephemeral'
              and pv['spec'].get('volumeMode','Filesystem')=='Filesystem'
              and pv['spec']['accessModes']==['ReadWriteOnce'],'published PV changed')
    terms=pv['spec']['nodeAffinity']['required']['nodeSelectorTerms']
    c.require(len(terms)==1 and not terms[0].get('matchFields'),'topology not exclusive')
    expressions=terms[0]['matchExpressions']
    c.require(any(x=={'key':'directpv.min.io/node','operator':'In','values':[policy['node']]} for x in expressions),'node constraint missing')
    c.require(all(x['key'].startswith('directpv.min.io/') and x['operator']=='In' and
                  x['values']==[node['metadata']['labels'].get(x['key'])] for x in expressions),'topology changed')
    return binding


def run(policy,receipt,config,path):
    c.require(not path.exists(),'fresh pool controller receipt required')
    policy={**policy,'initial_init_restarts':receipt['preflight']['init_restarts']}
    result={'complete':False,'created':[],'environment':policy['environment'],'started_at':pub.now().isoformat()}
    def journal():pub.write_journal(path,result)
    def call(args,data=None):
        r=subprocess.run(['kubectl','--kubeconfig',str(config),'--request-timeout=30s',*args],
             input=None if data is None else c.canonical(data).decode(),capture_output=True,text=True,timeout=45)
        c.require(r.returncode==0,'scoped pool operation failed');return json.loads(r.stdout) if r.stdout.strip() else None
    try:
        snap={key:call(args) for key,args in {
          'namespaces':['get','namespace','-o','json'],'nodes':['get','nodes','-o','json'],
          'sc':['get','sc','directpv-min-io-ephemeral','-o','json'],'drives':['get','directpvdrives','-o','json'],
          'pods':['get','pods','-A','-o','json'],'stats':['get','--raw','/api/v1/nodes/'+policy['node']+'/proxy/stats/summary']}.items()}
        result['preflight']=guard(policy,snap);journal()
        c.require(call(['get','pod',policy['name'],'-n','cicd','--ignore-not-found','-o','json']) is None,'publisher still present')
        pvc=call(['get','pvc',policy['name'],'-n','cicd','-o','json'])
        pv=call(['get','pv',pvc['spec']['volumeName'],'-o','json'])
        node=next(n for n in snap['nodes']['items'] if n['metadata']['name']==policy['node'])
        binding=volume(policy,receipt,pvc,pv,node)
        original=call(['get','autoscalingrunnerset','cs-'+policy['environment']+'-large-7c14g','-n','cicd','-o','json'])
        resource=pool(policy,binding,original);name=resource['metadata']['name']
        sets=call(['get','autoscalingrunnersets','-n','cicd','-o','json'])
        route=resource['spec']['runnerScaleSetLabels'][0]
        c.require(all(route not in x['spec'].get('runnerScaleSetLabels',[]) and
                      route!=x['spec'].get('runnerScaleSetName') for x in sets['items']), 'routing label already used')
        c.require(call(['get','autoscalingrunnerset',name,'-n','cicd','--ignore-not-found','-o','json']) is None,
                  'pool already exists; reconcile, never retry')
        result['original_pool_uid']=original['metadata']['uid'];result['binding']=binding
        result['pending_create']={'kind':'AutoscalingRunnerSet','name':name};journal()
        guard(policy,snap) # freshness gate immediately before the ONLY mutation
        created=call(['create','-f','-','-o','json'],resource)
        result['created']=[{'kind':'AutoscalingRunnerSet','name':name,'uid':created['metadata']['uid']}]
        result.pop('pending_create');result.update(complete=True,completed_at=pub.now().isoformat());journal()
        # Running/listener readiness and actual readonly runner mounts still
        # require independent checks before any full test dispatch.
        return result
    except Exception as error:
        import traceback
        result.update(error_type=type(error).__name__,automatic_retry=False,
          frames=[{'file':Path(f.filename).name,'line':f.lineno} for f in traceback.extract_tb(error.__traceback__)
                  if Path(f.filename).name.startswith('node_cache_')]);journal();raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('policy','publication','kubeconfig','report'):parser.add_argument('--'+key,required=True,type=Path)
    a=parser.parse_args()
    try:run(c.read_json(a.policy),c.read_json(a.publication),a.kubeconfig,a.report)
    except Exception as error:
        print(json.dumps({'complete':False,'error_type':type(error).__name__,'automatic_retry':False}));sys.exit(1)
