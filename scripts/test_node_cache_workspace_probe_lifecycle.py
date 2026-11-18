import copy
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import node_cache_workspace_probe_control as q
import test_node_cache_demand_plan as plan_fixture
import test_node_cache_publish_control as resource_fixture
import test_node_cache_workspace_probe_control as volume_fixture


class Lifecycle(unittest.TestCase):
    def exercise(self,fault=None):
        cat,policy,original,sc=plan_fixture.AllNodePlan().fixture()
        resource_policy,snapshot=resource_fixture.fixture();policy['namespace_uids']=resource_policy['namespace_uids']
        sc['metadata']['uid']='reviewed-class';mutations=[];sessions=[]
        node_rows=[n for n in cat['nodes'] if n['environment']=='mty']
        expected={'files':7,'bytes':99};evidence={'copies':[{'environment':'mty','targets':{'mo-standard-1':expected}}]}
        class Fake(q.Session):
            def __init__(self,*args):
                super().__init__(*args);sessions.append(self);self.counter=1000;self.ss_reads=0
                self.live={('autoscalingrunnerset',original['metadata']['name']):copy.deepcopy(original)}
                for binding in node_rows:
                    name='cs-mty-incremental-n'+binding['node'][-1]+'-v1';pvname=name+'-pv'
                    pvc={'metadata':{'name':name,'namespace':'cicd','uid':binding['pvc_uid']},
                        'spec':{'volumeName':pvname,'storageClassName':'directpv-min-io-ephemeral','accessModes':['ReadWriteOnce']},
                        'status':{'phase':'Bound','capacity':{'storage':'64Gi'}}}
                    pv={'metadata':{'name':pvname,'uid':binding['pv_uid']},'status':{'phase':'Bound'},'spec':{
                        'storageClassName':'directpv-min-io-ephemeral','accessModes':['ReadWriteOnce'],'csi':{'driver':'directpv-min-io'},
                        'claimRef':{'name':name,'namespace':'cicd','uid':binding['pvc_uid']},
                        'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchExpressions':[{
                            'key':'directpv.min.io/node','operator':'In','values':[binding['node']]}]}]}}}}
                    self.live['pvc',name]=pvc;self.live['pv',pvname]=pv
            def uid(self):
                self.counter+=1;return f'90000000-0000-4000-8000-{self.counter:012d}'
            def snapshot(self,node):
                value=copy.deepcopy(snapshot)
                value['nodes']['items']=[];value['drives']['items']=[]
                for binding in node_rows:
                    obj=copy.deepcopy(snapshot['nodes']['items'][0]);obj['metadata'].update(name=binding['node'],uid=binding['node_uid'])
                    obj['metadata']['labels']['directpv.min.io/node']=binding['node'];value['nodes']['items'].append(obj)
                    live_work=sum(1 for (kind,name),v in self.live.items() if kind=='pod' and v['spec']['nodeName']==binding['node'])
                    capacity=144 if fault=='one-slot' and binding['node']=='mo-standard-2' else 184
                    value['drives']['items'].append({'metadata':{'labels':obj['metadata']['labels']},
                        'status':{'status':'Ready','freeCapacity':(capacity-live_work*q.WORK_GIB)*q.c.GIB}})
                value['pods']={'items':[copy.deepcopy(v) for (kind,_),v in self.live.items() if kind=='pod']}
                value['sc']=copy.deepcopy(sc);value['quotas']={'items':[]}
                for key in ('cpu','memory','fs'):value['stats']['node'][key]['time']=q.pub.now().isoformat()
                return value
            def call(self,args,data=None,timeout=45):
                verb=args[0]
                if verb=='get':
                    key=(args[1].lower(),args[2]);value=self.live.get(key)
                    if key[0]=='autoscalingrunnerset':
                        self.ss_reads+=1
                        if fault=='original' and self.ss_reads==2:
                            value=copy.deepcopy(value);value['spec']['minRunners']=99
                    return copy.deepcopy(value)
                if verb=='create':
                    obj=copy.deepcopy(data);kind=obj['kind'];name=obj['metadata']['name'];obj['metadata']['uid']=self.uid()
                    mutations.append(('create',kind,name));self.live[kind.lower(),name]=obj
                    if kind=='Pod':
                        node=obj['spec']['nodeSelector']['kubernetes.io/hostname'];obj['spec']['nodeName']=node
                        obj['status']={'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}
                        _,pvc,pv,_=volume_fixture.ProbeChecks().volume();pvcname=name+'-work';pvname=pvcname+'-pv'
                        pvc['metadata'].update(name=pvcname,uid=self.uid())
                        pvc['metadata']['ownerReferences'][0].update(name=name,uid=obj['metadata']['uid'])
                        pvc['spec']['volumeName']=pvname
                        pv['metadata'].update(name=pvname,uid=self.uid())
                        pv['spec']['claimRef'].update(name=pvcname,uid=pvc['metadata']['uid'])
                        pv['spec']['nodeAffinity']['required']['nodeSelectorTerms'][0]['matchExpressions'][0]['values']=[node]
                        self.live['pvc',pvcname]=pvc;self.live['pv',pvname]=pv
                        if fault=='unknown-create':raise subprocess.TimeoutExpired(['scoped-create'],45)
                    return copy.deepcopy(obj)
                if verb=='delete':
                    name=args[2].split('/')[-1];pod=self.live['pod',name]
                    assert data['preconditions']['uid']==pod['metadata']['uid'] and '/pods/' in args[2]
                    mutations.append(('delete','Pod',name));self.live.pop(('pod',name))
                    # Simulate KUBERNETES owner-GC, not a controller API delete.
                    pvc=self.live.pop(('pvc',name+'-work'));self.live.pop(('pv',pvc['spec']['volumeName']))
                    return {'status':'Success'}
                assert verb=='exec';time.sleep(.01)
                script=args.index('/probe/node_cache_workspace_probe_job.py');mode=args[script+1]
                if mode=='recheck-private':return {'private_marker_verified':True,'tmp_on_work_filesystem':True}
                binding=json.loads(args[args.index('--binding-json')+1])
                return {'success':True,'auxiliary_probe_only':True,'binding':binding,'cpu_compatible':True,
                    'cache_write_denied_errno':'EROFS','compiler_invocations':0,'external_tool_downloads':0,
                    'shared_payload_files':7,'shared_payload_bytes':99,
                    'persistent_tools':{'persistent_tools':True,'external_tool_downloads':0,'binding':binding},
                    'private_work':{'private_marker_verified':True,'tmp_on_work_filesystem':True}}
        with tempfile.TemporaryDirectory() as raw,patch.object(q,'Session',Fake),patch.object(q,'authenticate') as authorize:
            directory=Path(raw).resolve()/'intent'
            if fault in ('original','unknown-create'):
                with self.assertRaises((ValueError,subprocess.TimeoutExpired)):
                    q.run(policy,cat,{},evidence,'private',directory)
                result=json.loads((directory/'state.json').read_text());self.assertFalse(result['complete'])
                if fault=='unknown-create':
                    self.assertEqual(result['pending_create']['kind'],'Pod')
                    self.assertEqual(len([m for m in mutations if m[:2]==('create','Pod')]),1)
                    self.assertEqual([m for m in mutations if m[0]=='delete'],[])
                return
            result=q.run(policy,cat,{},evidence,'private',directory)
            authorize.assert_called_once()
        self.assertTrue(result['complete']);self.assertTrue(result['work_claims_collected'])
        self.assertEqual(len(result['created']),10);self.assertEqual(len(result['deleted']),9)
        self.assertEqual(len(result['workspace_gc']),9);self.assertEqual(len(result['targets']),3)
        self.assertEqual({m[1] for m in mutations if m[0]=='create'},{'ConfigMap','Pod'})
        self.assertEqual({m[1] for m in mutations if m[0]=='delete'},{'Pod'})
        self.assertEqual(len({w['pvc_uid'] for w in result['workspaces']}),9)
        self.assertEqual(sessions[0].live['autoscalingrunnerset',original['metadata']['name']],original)
        self.assertFalse(any(kind=='pod' for kind,_ in sessions[0].live))
        self.assertEqual(len([1 for kind,_ in sessions[0].live if kind=='pvc']),3)
        if fault=='one-slot':
            target=result['targets']['mo-standard-2']
            self.assertEqual(target['simultaneous_workspaces'],1)
            self.assertEqual(target['capacity_wait']['reason'],'physical_workspace_reservation')
            self.assertFalse(target['capacity_wait']['runner_count_limit'])
            self.assertLessEqual(target['probes'][0]['completed_at'],target['probes'][1]['started_at'])

    def test_three_nodes_concurrent_pairs_replacements_and_owner_gc(self):self.exercise()
    def test_unknown_create_response_preserves_pending_no_retry(self):self.exercise('unknown-create')
    def test_original_pool_change_is_rejected(self):self.exercise('original')
    def test_one_slot_node_queues_until_owner_gc_without_runner_count_limit(self):self.exercise('one-slot')


if __name__=='__main__':unittest.main()
