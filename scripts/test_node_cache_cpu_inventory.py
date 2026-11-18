import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import node_cache_cpu_inventory as cpu
import node_cache_cpu_inventory_control as control
import test_node_cache_demand_plan as plans
import test_node_cache_publish_control as resources


CPU={'vendor_id':'GenuineIntel','cpu family':'6','model':'85','model name':'reviewed CPU','stepping':'7','flags':'fpu sse sse2 avx2'}


def catalog():
    cat,policy,original,sc=plans.AllNodePlan().fixture()
    for entry in cat['entries']:entry['runtime']['cpu']=copy.deepcopy(CPU)
    return cat,policy,original,sc


class Inventory(unittest.TestCase):
    def reports(self):
        cat,_,_,_=catalog();rows=[]
        for binding in cat['nodes']:
            actual=copy.deepcopy(CPU)
            if binding['environment']=='mty' and binding['node']!='mo-standard-0':actual.update(model='106',stepping='6')
            with patch.object(cpu.tools,'binding',return_value=binding):
                rows.append(cpu.inventory(cat,Path('/ci-cache'),binding['environment'],binding['node'],{},actual))
        return cat,rows

    def test_missing_cpu_observed_without_relaxing_compatibility_or_writes(self):
        cat,rows=self.reports();plan=cpu.plan(cat,rows)
        self.assertEqual(plan['observed_nodes'],12);self.assertFalse(plan['compatibility_aliases'])
        self.assertFalse(plan['full_runtime_acceptance']);self.assertFalse(plan['automatic_publication'])
        self.assertEqual(len(plan['missing_cpu_profiles']),1)
        self.assertEqual(plan['missing_cpu_profiles'][0]['nodes'],['mo-standard-1','mo-standard-2'])
        self.assertTrue(all(r['filesystem_writes']==0 for r in rows))

    def test_missing_duplicate_identity_hash_or_forged_match_refused(self):
        for fault in ('missing','duplicate','identity','hash','match','cpu'):
            cat,rows=self.reports()
            if fault=='missing':rows.pop()
            if fault=='duplicate':rows[-1]=copy.deepcopy(rows[0])
            if fault=='identity':rows[0]['binding']=dict(rows[0]['binding'],pvc_uid='foreign')
            if fault=='hash':rows[0]['cpu_sha256']='0'*64
            if fault=='match':rows[0]['exact_cpu_profile_present']=False
            if fault=='cpu':rows[0]['cpu']['model']='invalid'
            with self.subTest(fault=fault),self.assertRaises(ValueError):cpu.plan(cat,rows)

    def test_binding_failure_is_not_recorded_as_a_cold_cpu_profile(self):
        cat,_,_,_=catalog()
        with patch.object(cpu.tools,'binding',side_effect=ValueError('binding')):
            with self.assertRaises(ValueError):cpu.inventory(cat,Path('/ci-cache'),'mty','mo-standard-0',{},CPU)


class Controller(unittest.TestCase):
    def exercise(self,fault=None):
        q=control.q;cat,policy,original,sc=catalog();rp,snap=resources.fixture()
        policy['namespace_uids']=rp['namespace_uids'];sc['metadata']['uid']='class-id'
        bindings={b['node']:b for b in cat['nodes'] if b['environment']=='mty'};mutations=[]
        class Fake(q.Session):
            def __init__(self,*args):
                super().__init__(*args);self.next=100;self.live={};self.old_reads=0
            def snapshot(self,node):
                value=copy.deepcopy(snap);value['nodes']['items']=[];value['drives']['items']=[]
                for b in bindings.values():
                    n=copy.deepcopy(snap['nodes']['items'][0]);n['metadata'].update(name=b['node'],uid=b['node_uid'])
                    n['metadata']['labels']['directpv.min.io/node']=b['node'];value['nodes']['items'].append(n)
                    value['drives']['items'].append({'metadata':{'labels':n['metadata']['labels']},'status':{'status':'Ready','freeCapacity':144*q.c.GIB}})
                value.update(sc=copy.deepcopy(sc),quotas={'items':[]},pods={'items':[v for (kind,_),v in self.live.items() if kind=='pod']})
                for k in ('cpu','memory','fs'):value['stats']['node'][k]['time']=q.pub.now().isoformat()
                return value
            def cache_volume(self,binding):
                assert binding==bindings[binding['node']]
                if fault=='cache':raise ValueError('cache identity changed')
            def call(self,args,data=None,timeout=45):
                if args[0]=='get':
                    if args[1]=='autoscalingrunnerset':
                        self.old_reads+=1;result=copy.deepcopy(original)
                        if fault=='original' and self.old_reads==2:result['metadata']['uid']='replacement'
                        return result
                    return copy.deepcopy(self.live.get((args[1].lower(),args[2])))
                if args[0]=='create':
                    obj=copy.deepcopy(data);self.next+=1;obj['metadata']['uid']=f'90000000-0000-4000-8000-{self.next:012d}'
                    kind=obj['kind'];name=obj['metadata']['name'];self.live[kind.lower(),name]=obj;mutations.append(('create',kind))
                    if kind=='Pod':
                        obj['spec']['nodeName']=obj['spec']['nodeSelector']['kubernetes.io/hostname']
                        obj['status']={'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}
                        assert obj['spec']['containers'][0]['securityContext']['readOnlyRootFilesystem']
                        assert all(v['persistentVolumeClaim']['readOnly'] for v in obj['spec']['volumes'] if 'persistentVolumeClaim' in v)
                        assert not any('ephemeral' in v for v in obj['spec']['volumes'])
                        if fault=='create':raise subprocess.TimeoutExpired('create',45)
                    return copy.deepcopy(obj)
                if args[0]=='exec':
                    b=json.loads(args[args.index('--binding-json')+1]);actual=copy.deepcopy(CPU)
                    if b['node']=='mo-standard-1':actual['model']='106'
                    with patch.object(cpu.tools,'binding',return_value=b):return cpu.inventory(cat,Path('/ci-cache'),'mty',b['node'],{},actual)
                assert args[0]=='delete';name=args[2].split('/')[-1];pod=self.live['pod',name]
                assert data['preconditions']['uid']==pod['metadata']['uid']
                self.live.pop(('pod',name));mutations.append(('delete','Pod'));return {'status':'Success'}
        with tempfile.TemporaryDirectory() as raw,patch.object(control.q,'Session',Fake),patch.object(control.q,'authenticate'):
            directory=Path(raw).resolve()/'intent'
            if fault:
                with self.assertRaises((ValueError,subprocess.TimeoutExpired)):control.run(policy,cat,{}, {},'private',directory)
                r=json.loads((directory/'state.json').read_text());self.assertFalse(r['complete'])
                if fault=='create':
                    self.assertIn('pending_create',r);self.assertEqual(mutations,[('create','ConfigMap'),('create','Pod')])
                if fault=='cache':self.assertEqual(mutations,[])
                return
            result=control.run(policy,cat,{}, {},'private',directory)
            self.assertTrue(result['complete']);self.assertEqual(len(result['targets']),3)
            self.assertEqual(mutations,[('create','ConfigMap')]+[v for _ in range(3) for v in [('create','Pod'),('delete','Pod')]])
            self.assertFalse(result['targets']['mo-standard-1']['report']['exact_cpu_profile_present'])
            self.assertEqual(result['filesystem_writes'],0)

    def test_three_nodes_zero_pvc_writes_and_uid_cleanup(self):self.exercise()
    def test_unknown_create_not_retried_or_cleaned_blindly(self):self.exercise('create')
    def test_cache_replacement_fails_before_creation(self):self.exercise('cache')
    def test_original_pool_replacement_rejected(self):self.exercise('original')


if __name__=='__main__':unittest.main()
