import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch,MagicMock

import node_cache_append_control as a
from test_node_cache_publish_control import fixture,U


class AppendControllerTests(unittest.TestCase):
    def fixture(self):
        policy,snapshot=fixture()
        binding={'environment':'mty','node':'mo-standard-0','node_uid':U,'pvc_uid':U,'pv_uid':U}
        full={**binding,'marker_sha256':'a'*64}
        receipt={'complete':True,'verify':{'success':True,'binding':full},'publish':{'binding':full}}
        base={'binding':binding,'marker_sha256':'a'*64,'indexes':['b'*64],'native_manifests':[]}
        entry={'runtime':{'environment':'mty'},'bindings':[],'producer':{'id':1},'artifact':{'id':2}}
        pvc={'metadata':{'name':policy['name'],'namespace':'cicd','uid':U},'status':{'phase':'Bound'},
             'spec':{'volumeName':'existing-pv','storageClassName':'directpv-min-io-ephemeral','accessModes':['ReadWriteOnce']}}
        pv={'metadata':{'name':'existing-pv','uid':U},'status':{'phase':'Bound'},'spec':{
            'claimRef':{'name':policy['name'],'namespace':'cicd','uid':U},'csi':{'driver':'directpv-min-io'},
            'storageClassName':'directpv-min-io-ephemeral','accessModes':['ReadWriteOnce'],
            'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchExpressions':[
                {'key':k,'operator':'In','values':[v]} for k,v in snapshot['nodes']['items'][0]['metadata']['labels'].items()]}]}}}}
        return policy,snapshot,receipt,base,entry,pvc,pv

    def lifecycle(self,case=None,resuming=False):
        policy,s,receipt,base,entry,pvc,pv=self.fixture();live={};writes=[]
        original_pvc=copy.deepcopy(pvc);original_pv=copy.deepcopy(pv)
        if case=='wrong-pvc':pvc['metadata']['uid']='different'
        if case=='runner':s['pods']['items']=[{'metadata':{'ownerReferences':[{'kind':'EphemeralRunner'}]},'status':{'phase':'Pending'}}]
        if case=='root':s['stats']['node']['fs']['availableBytes']=59*a.c.GIB
        resume=None
        if resuming:
            name='cs-mty-nc-append-1-2'
            resources=a.objects(policy,entry,base,name)
            for obj in resources:
                obj['metadata']['uid']=U;live[obj['kind']]=obj
            live['Pod']['spec']['nodeName']=policy['node']
            live['Pod']['status']={'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}
            resume={'complete':False,'environment':'mty','name':name,'compiler_invocations':0,
                'existing_pvc_preserved':True,'started_at':'previous-reviewed-attempt',
                'created':[{'kind':k,'name':name,'uid':U} for k in ('ConfigMap','Pod')]}
        def execute(cmd,**kwargs):
            if 'wait' in cmd:return subprocess.CompletedProcess(cmd,0,'pod condition met','')
            args=cmd[4:];verb=args[0];result=None
            if verb=='get':
                kind=args[1]
                values={'namespace':s['namespaces'],'nodes':s['nodes'],'node':s['nodes']['items'][0],
                        'sc':s['sc'],'directpvdrives':s['drives'],'--raw':s['stats'],'pvc':pvc,'pv':pv}
                if kind=='pods':result={'items':s['pods']['items']+([live['Pod']] if 'Pod' in live else [])}
                elif kind in values:result=values[kind]
                else:result=live.get({'pod':'Pod','configmap':'ConfigMap'}.get(kind,kind))
            elif verb=='create':
                obj=json.loads(kwargs['input']);kind=obj['kind'];self.assertIn(kind,('ConfigMap','Pod'))
                writes.append(('create',kind));obj['metadata']['uid']=U;live[kind]=obj
                if kind=='Pod':
                    obj['spec']['nodeName']=policy['node'];obj['status']={'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}
                    if case=='mutated-pod':obj['spec']['automountServiceAccountToken']=True
                    if case in ('defaults','wrong-toleration','wrong-field-api'):
                        for key in ('node.kubernetes.io/not-ready','node.kubernetes.io/unreachable'):
                            obj['spec']['tolerations'].append({'effect':'NoExecute','key':key,'operator':'Exists','tolerationSeconds':300})
                        obj['spec']['containers'][0]['env'][1]['valueFrom']['fieldRef']['apiVersion']='v1'
                        if case=='wrong-toleration':obj['spec']['tolerations'][-1]['tolerationSeconds']=600
                        if case=='wrong-field-api':obj['spec']['containers'][0]['env'][1]['valueFrom']['fieldRef']['apiVersion']='v2'
                result=obj
                if case=='ambiguous-create':raise subprocess.TimeoutExpired(cmd,60)
            elif verb=='exec':
                if '-c' in args:
                    writes.append(('check','empty-stage'))
                    return subprocess.CompletedProcess(cmd,0,json.dumps({'empty':case!='dirty-stage'}),'')
                phase=args[-1];writes.append(('exec',phase))
                if case=='failed-append' and phase=='append':raise RuntimeError('scoped execution failed')
                result={'success':True,'phase':phase}
            elif verb=='delete':
                self.assertEqual(json.loads(kwargs['input'])['preconditions']['uid'],U)
                self.assertEqual(args[2],'/api/v1/namespaces/cicd/pods/'+live['Pod']['metadata']['name'])
                live.pop('Pod');writes.append(('delete','Pod'));result={'status':'Success'}
            else:raise AssertionError(args)
            return subprocess.CompletedProcess(cmd,0,'' if result is None else json.dumps(result),'')
        opener=MagicMock();opener.open.side_effect=lambda *args,**kwargs:io.BytesIO(b'{}')
        with tempfile.TemporaryDirectory() as tmp,patch.object(a.subprocess,'run',side_effect=execute), \
             patch.object(a.urllib.request,'build_opener',return_value=opener),patch.object(a.authorization,'authorize'), \
             patch.object(a.transport,'signed_archive_url',return_value='https://fixture.blob.core.windows.net/object'), \
             patch.dict(os.environ,{'CS_CACHE_READ_TOKEN':'fixture'}):
            path=Path(tmp).resolve()/'append.json'
            if case and case!='defaults':
                with self.assertRaises(Exception):a.run(policy,entry,base,receipt,'fixture',path,resume)
                result=json.loads(path.read_text());self.assertFalse(result['complete'])
                self.assertFalse(result['automatic_retry'])
            else:result=a.run(policy,entry,base,receipt,'fixture',path,resume)
        if case!='wrong-pvc':self.assertEqual(pvc,original_pvc)
        self.assertEqual(pv,original_pv)
        return result,writes,live

    def test_complete_append_creates_only_cm_pod_and_uid_deletes_pod(self):
        result,writes,live=self.lifecycle()
        self.assertTrue(result['complete']);self.assertEqual(writes,[('create','ConfigMap'),('create','Pod'),
            ('exec','stage'),('exec','append'),('exec','verify'),('delete','Pod')])
        self.assertIn('ConfigMap',live);self.assertNotIn('Pod',live)

    def test_wrong_volume_active_runner_and_low_disk_reject_before_write(self):
        for case in ('wrong-pvc','runner','root'):
            with self.subTest(case=case):
                _,writes,_=self.lifecycle(case);self.assertEqual(writes,[])

    def test_uncertain_create_records_target_without_retry(self):
        result,writes,_=self.lifecycle('ambiguous-create')
        self.assertEqual(writes,[('create','ConfigMap')]);self.assertEqual(result['pending_create']['kind'],'ConfigMap')

    def test_mutated_pod_never_receives_download(self):
        _,writes,_=self.lifecycle('mutated-pod')
        self.assertFalse(any(x[0]=='exec' for x in writes))

    def test_failed_append_preserves_pod_and_journal_for_reconciliation(self):
        result,writes,live=self.lifecycle('failed-append')
        self.assertEqual(result['active_phase'],'append');self.assertIn('Pod',live)
        self.assertNotIn(('delete','Pod'),writes)

    def test_resources_mount_only_existing_cache_and_private_stage(self):
        policy,_,_,base,entry,_,_=self.fixture()
        resources=a.objects(policy,entry,base,'cs-mty-nc-append-1-2')
        self.assertEqual([x['kind'] for x in resources],['ConfigMap','Pod'])
        pod=resources[-1]['spec'];self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['volumes'][0]['persistentVolumeClaim']['claimName'],policy['name'])
        self.assertEqual(pod['containers'][0]['resources']['limits'],{'cpu':'1','memory':'1Gi'})
        self.assertNotIn('TOKEN',str(pod['containers'][0]['env']))
        with self.assertRaises(ValueError):a.objects(policy,entry,base,'cs-mty-nc-append-1-3')

    def test_observed_api_defaults_only_are_accepted(self):
        self.assertTrue(self.lifecycle('defaults')[0]['complete'])
        for case in ('wrong-toleration','wrong-field-api'):
            _,writes,_=self.lifecycle(case)
            self.assertFalse(any(w[0]=='exec' for w in writes))

    def test_explicit_pretransfer_resume_creates_no_resources(self):
        result,writes,_=self.lifecycle(resuming=True)
        self.assertTrue(result['complete']);self.assertTrue(result['resumed_existing_resources'])
        self.assertEqual(writes,[('check','empty-stage'),('exec','stage'),('exec','append'),('exec','verify'),('delete','Pod')])

    def test_dirty_or_ambiguous_stage_never_retried(self):
        _,writes,live=self.lifecycle('dirty-stage',resuming=True)
        self.assertEqual(writes,[('check','empty-stage')]);self.assertIn('Pod',live)
        for key in ('pending_create','pending_delete','active_phase','stage','append','verify'):
            with self.assertRaises(ValueError):a.resume_created({'complete':False,'environment':'mty',
                'name':'n','compiler_invocations':0,'existing_pvc_preserved':True,key:{}},'mty','n')


if __name__=='__main__':unittest.main()
