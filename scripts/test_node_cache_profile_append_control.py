import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import node_cache_profile_append_control as p
import test_node_cache_demand_plan as plans
import test_node_cache_publish_control as resources
import test_node_cache_workspace_probe_control as volumes

CPU={'vendor_id':'GenuineIntel','cpu family':'6','model':'85','model name':'old CPU','stepping':'7','flags':'fpu sse2 avx2'}


class Control(unittest.TestCase):
    def test_writable_mount_defaults_survive_real_api_false_omission(self):
        cat,policy,original,sc,_,_,entry,base=self.fixture('mo-standard-1')
        plan=next(x for x in p.q.planner.plan(cat,policy,original,sc,80)
            if x['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname']=='mo-standard-1')
        expected=p.publisher_pod(plan,'writer','script');actual=copy.deepcopy(expected)
        actual['metadata']['uid']='90000000-0000-4000-8000-000000000001'
        actual['status']={'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}
        self.assertNotIn('readOnly',expected['spec']['volumes'][1]['persistentVolumeClaim'])
        self.assertNotIn('readOnly',expected['spec']['containers'][0]['volumeMounts'][1])
        self.assertTrue(p.checked_publisher_pod(expected,actual,actual['metadata']['uid']))
        old=copy.deepcopy(expected)
        old['spec']['volumes'][1]['persistentVolumeClaim']['readOnly']=False
        old['spec']['containers'][0]['volumeMounts'][1]['readOnly']=False
        with self.assertRaisesRegex(ValueError,'template changed'):
            p.q.checked_pod(old,actual,actual['metadata']['uid'])
        for fault in ('uid','image','mount','volume'):
            changed=copy.deepcopy(actual)
            if fault=='uid':changed['metadata']['uid']='other'
            if fault=='image':changed['spec']['containers'][0]['image']='other'
            if fault=='mount':changed['spec']['containers'][0]['volumeMounts'][1]['readOnly']=True
            if fault=='volume':changed['spec']['volumes'][1]['persistentVolumeClaim']['claimName']='other'
            with self.subTest(fault=fault),self.assertRaises(ValueError):
                p.checked_publisher_pod(expected,changed,actual['metadata']['uid'])

    def test_recovery_scope_rejects_any_staged_or_ambiguous_old_operation(self):
        name='cs-mty-nc-profile-n1-44326799783-14440043306'
        receipt={'complete':False,'error_type':'ValueError','name':name,'node':'mo-standard-1',
            'failure_sites':[{'file':'node_cache_profile_append_control.py','line':126},
                {'file':'node_cache_workspace_probe_control.py','line':203},
                {'file':'node_cache_workspace_probe_control.py','line':53},
                {'file':'incremental_cache.py','line':29}],
            'provenance_verified_before_write':True,'deleted':[],
            'created':[{'kind':'ConfigMap','name':name,'uid':'cm-id'},
                {'kind':'Pod','name':name+'-writer','uid':'pod-id'}]}
        artifact={'id':14442721595,'name':'node-cache-profile-append-mty-44338220751','size_in_bytes':2598,
            'digest':'sha256:64478db06c8f1a9d94f67cde6c555975eec9e3d1a96f45124bf9b7b28952807f'}
        approval={'schema':1,'run_id':44338220751,'gitops_revision':'5dacdfb7d951c70c42db216606672f7dd9d61586',
            'receipt':receipt,'artifact':artifact,'canonical_report_sha256':p.core.cache.hash_value(receipt)}
        entry={'producer':{'id':44326799783}}
        self.assertEqual(p.recovery_scope(approval,entry,name),receipt)
        for fault in ('stage','phase','workspace','pending','hash','run','artifact','failure'):
            changed=copy.deepcopy(approval);r=changed['receipt']
            if fault=='stage':r['stage']={'success':False}
            if fault=='phase':r['active_phase']='stage'
            if fault=='workspace':r['workspace']={}
            if fault=='pending':r['pending_create']={}
            if fault=='hash':changed['canonical_report_sha256']='0'*64
            if fault=='run':changed['run_id']+=1
            if fault=='artifact':changed['artifact']['digest']='sha256:'+'0'*64
            if fault=='failure':r['failure_sites']=[]
            if fault!='hash':changed['canonical_report_sha256']=p.core.cache.hash_value(r)
            with self.subTest(fault=fault),self.assertRaises(ValueError):p.recovery_scope(changed,entry,name)

    def fixture(self,node='mo-standard-2'):
        cat,policy,original,sc=plans.AllNodePlan().fixture()
        rp,snap=resources.fixture();policy['namespace_uids']=rp['namespace_uids'];sc['metadata']['uid']='class-id'
        for entry in cat['entries']:
            if entry['runtime']['environment']=='mty':
                entry['runtime']['cpu']=copy.deepcopy(CPU)
                entry['expires_at']='2099-01-01T00:00:00Z'
        binding=next(b for b in cat['nodes'] if b['environment']=='mty' and b['node']==node)
        parents=p.core.cache.catalog_for_node(cat,binding)['entries']
        entry=copy.deepcopy(parents[0]);entry['runtime']['cpu'].update(model='106',stepping='6')
        entry.update(index_sha256='f'*64,bindings=[]);entry['producer']['id']=9001;entry['artifact']['id']=9002
        base=p.core.approval(binding,parents,entry)
        return cat,policy,original,sc,snap,binding,entry,base

    def test_only_independently_approved_node_and_exact_cpu_allowed(self):
        cat,policy,_,_,_,binding,entry,base=self.fixture()
        expected=p.core.cpus.checked_cpu(entry['runtime']['cpu'])
        with patch.dict(p.PROFILES,{binding['node']:expected}):
            self.assertEqual(p.scope(policy,cat,entry,base)[0],binding)
            altered=copy.deepcopy(base);altered['marker_sha256']='0'*64
            with self.assertRaises(ValueError):p.scope(policy,cat,entry,altered)
        with self.assertRaises(ValueError):p.scope(policy,cat,entry,base)

    def exercise(self,fault=None,node='mo-standard-2',adopt=False):
        q=p.q;cat,policy,original,sc,snapshot,binding,entry,base=self.fixture(node)
        bindings={b['node']:b for b in cat['nodes'] if b['environment']=='mty'};writes=[];calls=[]
        name=f"cs-mty-nc-profile-n{node[-1]}-{entry['producer']['id']}-{entry['artifact']['id']}"
        operation=name
        old={'created':[{'kind':'ConfigMap','name':name,'uid':'90000000-0000-4000-8000-000000000101'},
            {'kind':'Pod','name':name+'-writer','uid':'90000000-0000-4000-8000-000000000102'}]}
        api_values={}
        for approved in base['existing_entries']+[entry]:
            producer=approved['producer'];artifact=approved['artifact']
            api_values[f"repos/{producer['repository']}/actions/runs/{producer['id']}"]={
                **producer,'repository':{'full_name':producer['repository']},'status':'completed','conclusion':'success',
                'actor':{'login':producer['actor']},'triggering_actor':{'login':producer['actor']}}
            api_values[f"repos/{producer['repository']}/actions/artifacts/{artifact['id']}"]={
                **artifact,'expired':False,'workflow_run':{'id':producer['id'],'head_sha':producer['head_sha']}}
        api_values[f'repos/matrixorigin/cloudsigma-gitops/actions/runs/{p.CPU_RUN}']={
            'id':p.CPU_RUN,'head_sha':p.CPU_REV,'head_branch':'codex/ruh-pvc-readiness','event':'workflow_dispatch',
            'run_attempt':1,'status':'completed','conclusion':'success','actor':{'login':'Ariznawlll'},
            'triggering_actor':{'login':'Ariznawlll'},'path':'.github/workflows/cloudsigma-up.yaml',
            'display_title':'up cs-component / mty / node-cache-cpu-inventory'}
        api_values[f'repos/matrixorigin/cloudsigma-gitops/actions/artifacts/{p.CPU_ARTIFACT}']={
            'id':p.CPU_ARTIFACT,'digest':p.CPU_DIGEST,'expired':False,'workflow_run':{'id':p.CPU_RUN,'head_sha':p.CPU_REV}}
        if fault=='producer':api_values[f"repos/matrixorigin/matrixone/actions/runs/{entry['producer']['id']}"]['head_sha']='0'*40
        if fault=='census':api_values[f'repos/matrixorigin/cloudsigma-gitops/actions/artifacts/{p.CPU_ARTIFACT}']['digest']='sha256:'+'0'*64
        if fault=='parent':api_values[f"repos/matrixorigin/matrixone/actions/artifacts/{base['existing_entries'][0]['artifact']['id']}"]['expired']=True
        def api(path):calls.append(path);return copy.deepcopy(api_values[path])
        class Fake(q.Session):
            def __init__(self,*args):
                super().__init__(*args);self.live={};self.next=100;self.pool_reads=0
                for b in bindings.values():
                    name='cs-mty-incremental-n'+b['node'][-1]+'-v1';pvname=name+'-pv'
                    self.live['pvc',name]={'metadata':{'name':name,'namespace':'cicd','uid':b['pvc_uid']},
                        'spec':{'volumeName':pvname,'storageClassName':'directpv-min-io-ephemeral','accessModes':['ReadWriteOnce']},
                        'status':{'phase':'Bound','capacity':{'storage':'64Gi'}}}
                    self.live['pv',pvname]={'metadata':{'name':pvname,'uid':b['pv_uid']},'status':{'phase':'Bound'},
                        'spec':{'storageClassName':'directpv-min-io-ephemeral','accessModes':['ReadWriteOnce'],
                            'csi':{'driver':'directpv-min-io'},'claimRef':{'name':name,'namespace':'cicd','uid':b['pvc_uid']},
                            'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchExpressions':[
                                {'key':'directpv.min.io/node','operator':'In','values':[b['node']]}]}]}}}}
                if adopt:
                    planned=next(x for x in q.planner.plan(cat,policy,original,sc,80)
                        if x['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname']==node)
                    self.call(['create'],{'kind':'ConfigMap','metadata':{'name':operation},'immutable':True,'data':p.instructions(entry,base)})
                    self.call(['create'],p.publisher_pod(planned,operation+'-writer',operation))
                    writes.clear()
            def uid(self):self.next+=1;return f'90000000-0000-4000-8000-{self.next:012d}'
            def snapshot(self,target):
                s=copy.deepcopy(snapshot);s['nodes']['items']=[];s['drives']['items']=[]
                for b in bindings.values():
                    n=copy.deepcopy(snapshot['nodes']['items'][0]);n['metadata'].update(name=b['node'],uid=b['node_uid'])
                    n['metadata']['labels']['directpv.min.io/node']=b['node']
                    if b['node']=='mo-standard-2':n['status']['allocatable']['memory']='18Gi'
                    s['nodes']['items'].append(n)
                    count=sum(1 for (k,_),v in self.live.items() if k=='pod' and v['spec']['nodeName']==b['node'])
                    free=99 if fault=='disk' and b['node']==node else 144-count*q.WORK_GIB
                    s['drives']['items'].append({'metadata':{'labels':n['metadata']['labels']},
                        'status':{'status':'Ready','freeCapacity':free*q.c.GIB}})
                s.update(sc=copy.deepcopy(sc),quotas={'items':[]},
                    pods={'items':[v for (k,_),v in self.live.items() if k=='pod']})
                s['stats']['node']['memory']['availableBytes']=(3 if fault=='memory' else 16)*q.c.GIB
                for k in ('cpu','memory','fs'):s['stats']['node'][k]['time']=q.pub.now().isoformat()
                return s
            def call(self,args,data=None,timeout=45):
                if args[0]=='get':
                    if args[1]=='autoscalingrunnerset':
                        self.pool_reads+=1;value=copy.deepcopy(original)
                        if fault=='original' and self.pool_reads==2:value['metadata']['uid']='changed'
                        return value
                    return copy.deepcopy(self.live.get((args[1].lower(),args[2])))
                if args[0]=='create':
                    value=copy.deepcopy(data);kind=value['kind'];name=value['metadata']['name'];value['metadata']['uid']=self.uid()
                    writes.append(('create',kind));self.live[kind.lower(),name]=value
                    if kind=='Pod':
                        value['metadata']['creationTimestamp']=q.pub.now().isoformat()
                        value['spec']['nodeName']=value['spec']['nodeSelector']['kubernetes.io/hostname']
                        value['status']={'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}
                        _,pvc,pv,_=volumes.ProbeChecks().volume();pvname=name+'-work-pv'
                        pvc['metadata'].update(name=name+'-work',uid=self.uid())
                        pvc['metadata']['ownerReferences'][0].update(name=name,uid=value['metadata']['uid'])
                        pvc['spec']['volumeName']=pvname;pv['metadata'].update(name=pvname,uid=self.uid())
                        pv['spec']['claimRef'].update(name=name+'-work',uid=pvc['metadata']['uid'])
                        pv['spec']['nodeAffinity']['required']['nodeSelectorTerms'][0]['matchExpressions'][0]['values']=[node]
                        if fault=='workspace':pvc['metadata']['ownerReferences'][0]['uid']='foreign'
                        self.live['pvc',name+'-work']=pvc;self.live['pv',pvname]=pv
                        if fault=='create':raise subprocess.TimeoutExpired('create',45)
                    return copy.deepcopy(value)
                if args[0]=='exec':
                    if args[-2]=='-c':
                        writes.append(('exec','readonly-empty-stage'));return {'stage_empty':True}
                    phase=args[-1];writes.append(('exec',phase))
                    assert data['binding']==base['binding'] and len(data['parent_authorizations'])==len(base['existing_entries'])
                    for parent,proof in zip(base['existing_entries'],data['parent_authorizations']):
                        assert parent['index_sha256']==proof['index_sha256'] and proof['binding']==base['binding']
                    if fault=='phase' and phase=='append':raise ValueError('publication uncertain')
                    return {'success':True,'phase':phase,'compiler_invocations':0,'independently_verified':True,
                        'compatibility_aliases':False,'binding':base['binding'],'marker_sha256':base['marker_sha256']}
                assert args[0]=='delete';name=args[2].split('/')[-1];pod=self.live['pod',name]
                assert data['preconditions']['uid']==pod['metadata']['uid']
                writes.append(('delete','Pod'));self.live.pop(('pod',name))
                pvc=self.live.pop(('pvc',name+'-work'));self.live.pop(('pv',pvc['spec']['volumeName']))
                return {'status':'Success'}
        with tempfile.TemporaryDirectory() as raw,patch.object(q,'Session',Fake),patch.object(q,'authenticate'), \
            patch.dict(p.PROFILES,{node:p.core.cpus.checked_cpu(entry['runtime']['cpu'])}), \
            patch.object(p,'recovery_authority',return_value=old), \
            patch.object(p.transport,'signed_archive_url',return_value='https://fixture.blob.core.windows.net/object'):
            directory=Path(raw).resolve()/'intent'
            if fault:
                with self.assertRaises((ValueError,subprocess.TimeoutExpired)):
                    p.run(policy,cat,{}, {},entry,base,'private',directory,api)
                result=json.loads((directory/'state.json').read_text());self.assertFalse(result['complete'])
                self.assertFalse(result['automatic_retry'])
                if fault in ('producer','parent','census','disk','memory'):self.assertEqual(writes,[])
                if fault=='create':self.assertEqual(writes,[('create','ConfigMap'),('create','Pod')]);self.assertIn('pending_create',result)
                if fault=='workspace':self.assertFalse(any(w[0]=='exec' for w in writes))
                if fault=='phase':self.assertEqual(result['active_phase'],'append');self.assertNotIn(('delete','Pod'),writes)
                return
            result=p.run(policy,cat,{}, {},entry,base,'private',directory,api,
                recovery={'run_id':44338220751,'canonical_report_sha256':'a'*64} if adopt else None)
            self.assertTrue(result['complete']);self.assertTrue(result['private_stage_collected'])
            self.assertEqual(writes,([('exec','readonly-empty-stage')] if adopt else [('create','ConfigMap'),('create','Pod')])+
                [('exec','stage'),('exec','append'),('exec','verify'),('delete','Pod')])
            if adopt:
                self.assertEqual(result['adopted_resources'],old['created'])
                self.assertTrue(result['recovery_of']['empty_private_stage_verified'])
            self.assertFalse(result['catalog_activated']);self.assertEqual(len(result['workspace_gc']),1)
            self.assertGreater(len(calls),len(base['existing_entries'])*2)

    def test_node2_realistic_small_memory_does_not_inherit_old_node0_32g_gate(self):self.exercise()
    def test_node1_publisher_same_cache_new_private_stage(self):self.exercise(node='mo-standard-1')
    def test_recovery_adopts_exact_existing_resources_no_new_pvc_pod_and_checks_empty_stage(self):
        self.exercise(node='mo-standard-1',adopt=True)
    def test_failure_guards_and_unknown_writes_never_retry(self):
        for fault in ('producer','parent','census','disk','memory','create','workspace','phase','original'):
            with self.subTest(fault=fault):self.exercise(fault)

    def test_projected_configmap_imports_all_dependencies_without_checkout(self):
        _,_,_,_,_,_,entry,base=self.fixture()
        data=p.instructions(entry,base)
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw).resolve()
            for name,text in data.items():(path/name).write_text(text)
            result=subprocess.run([sys.executable,'-I','-c',
                'import sys;sys.path.insert(0,sys.argv[1]);import node_cache_profile_append_job',str(path)],
                cwd=path,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
