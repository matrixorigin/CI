import copy
from pathlib import Path
import unittest
from unittest.mock import patch

import incremental_cache as c
import node_cache_native_append_control as native

ROOT=Path(__file__).resolve().parents[1]/'config'


class NativePolicy(unittest.TestCase):
    def setUp(self):
        self.original=c.read_json(ROOT/'node-compile-cache-catalog.json')
        self.active=c.read_json(ROOT/'node-compile-cache-pr-catalog.json')
        self.proof=c.read_json(ROOT/'node-cache-pr-profile-proof.json')
        self.request=c.read_json(ROOT/'node-native-refresh-request.json')
        self.operation=native.NativeOnlyPolicy(self.request,self.original,self.proof)
        self.policy={'environment':'mty','node_uids':{b['node']:b['node_uid'] for b in self.active['nodes'] if b['environment']=='mty'}}

    def fixture(self,node):
        binding=next(b for b in self.active['nodes'] if (b['environment'],b['node'])==('mty',node))
        profile=self.request['profiles'][node]
        entry={'index_sha256':'a'*64,'runtime':profile['runtime'],'bindings':[],
            'native_only':True,'compile_only':True,'source':native.SOURCE,
            'native_manifests':{profile['native_key']:'b'*64},'artifact':{'id':2},
            'producer':{'id':1,'repository':'matrixorigin/matrixone','head_sha':native.PRODUCER_CALLER,
                'head_branch':'weilu-runner-test','event':'workflow_dispatch','actor':'Ariznawlll','run_attempt':1,
                'path':'.github/workflows/race-seed-canary.yaml','referenced_workflows':[{
                    'sha':native.PRODUCER_CI,'path':'matrixorigin/CI/.github/workflows/node-cache-native-refresh.yaml@'+native.PRODUCER_CI}]}}
        base=native.engine.core.approval(binding,native.cache.catalog_for_node(self.active,binding)['entries'],entry)
        return entry,base

    def test_new_policy_exact_nodes_and_original_cpu_scope_unchanged(self):
        self.assertEqual(set(native.engine.PROFILES),{'mo-standard-1','mo-standard-2'})
        self.assertEqual(set(self.operation.profiles),{'mo-standard-0','mo-standard-1'})
        for node in self.operation.profiles:
            entry,base=self.fixture(node)
            binding,name=self.operation.scope(self.policy,self.active,entry,base)
            self.assertEqual(binding['node'],node);self.assertIn('-n'+node[-1]+'-',name)
        entry,base=self.fixture('mo-standard-0')
        with self.assertRaises(ValueError):native.engine.scope(self.policy,self.active,entry,base)

    def test_unreviewed_source_nativekey_producer_cpu_binding_or_parent_rejected(self):
        for fault in ('source','key','producer','actor','workflow','runtime','binding','parent','kind','node2'):
            entry,base=copy.deepcopy(self.fixture('mo-standard-0'))
            if fault=='source':entry['source']='0'*40
            if fault=='key':entry['native_manifests']={'0'*64:'b'*64}
            if fault=='producer':entry['producer']['head_sha']='0'*40
            if fault=='actor':entry['producer']['actor']='fixture-user'
            if fault=='workflow':entry['producer']['referenced_workflows']=[]
            if fault=='runtime':entry['runtime']['cpu']['model']='0'
            if fault=='binding':base['binding']['pvc_uid']='other'
            if fault=='parent':base['existing_entries']=base['existing_entries'][1:]
            if fault=='kind':entry['native_only']=False
            if fault=='node2':base['binding']['node']='mo-standard-2'
            with self.subTest(fault=fault),self.assertRaises(ValueError):self.operation.scope(self.policy,self.active,entry,base)

    def test_composite_base_keeps_original_copy_and_raw_append_authorities(self):
        approved=self.proof['append_approval'];expected=approved['controller_run']
        run={**expected,'run_attempt':1,'status':'completed','conclusion':'success','event':'workflow_dispatch',
            'head_branch':'codex/ruh-pvc-readiness','actor':{'login':'Ariznawlll'},'triggering_actor':{'login':'Ariznawlll'}}
        artifact={**approved['artifact'],'expired':False,'workflow_run':{'id':run['id'],'head_sha':run['head_sha']}}
        def api(path):return run if '/runs/' in path else artifact
        evidence=c.read_json(ROOT/'node-cache-all-node-copy-evidence.json');lock=c.read_json(ROOT/'node-toolchain-lock.json')
        with patch.object(native.engine.q,'authenticate') as auth:
            self.operation.authenticate(self.active,lock,evidence,api)
            self.assertEqual(auth.call_args.args[0],self.original)
            for fault in ('run','artifact','expired'):
                r,a=copy.deepcopy((run,artifact))
                if fault=='run':r['head_sha']='0'*40
                if fault=='artifact':a['digest']='sha256:'+'0'*64
                if fault=='expired':a['expired']=True
                with self.subTest(fault=fault),self.assertRaises(ValueError):
                    self.operation.authenticate(self.active,lock,evidence,lambda path:r if '/runs/' in path else a)


if __name__=='__main__':unittest.main()
