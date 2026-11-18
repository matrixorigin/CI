import copy
import datetime as dt
import hashlib
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import incremental_cache as c
import node_cache_profile_append as p
import node_compile_cache as cache
from test_node_compile_cache import approval

CPU={'vendor_id':'GenuineIntel','cpu family':'6','model':'85','model name':'old CPU','stepping':'7','flags':'fpu sse2 avx2'}


class Profiles(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
        self.shared=self.root/'shared';self.candidate=self.root/'candidate';self.candidate.mkdir()
        self.old_runtime={'environment':'mty','repository':'matrixorigin/matrixone','go':'fixture','cpu':copy.deepcopy(CPU)}
        self.new_runtime=copy.deepcopy(self.old_runtime);self.new_runtime['cpu'].update(model='106',**{'model name':'new CPU'},stepping='6')
        self.binding={'environment':'mty','node':'mo-standard-1','node_uid':'11111111-1111-1111-1111-111111111111',
            'pvc_uid':'22222222-2222-2222-2222-222222222222','pv_uid':'33333333-3333-3333-3333-333333333333'}
        c.atomic_json(self.shared/'node-cache-identity.json',self.binding)
        self.old_index={'schema':2,'runtime':self.old_runtime,'actions':{'a'*64:self.object(self.shared,b'old Go object')}}
        self.old_id=cache.hash_value(self.old_index);c.atomic_json(self.shared/'indexes'/(self.old_id+'.json'),self.old_index)
        self.old_entry,_=approval(self.old_id,self.old_runtime)
        self.old_entry['bindings']=[{k:self.binding[k] for k in ('node_uid','pvc_uid','pv_uid')}]
        self.index={'schema':2,'runtime':self.new_runtime,'actions':{
            'a'*64:self.object(self.candidate,b'CPU-specific object'),
            'b'*64:self.object(self.candidate,b'old Go object')}}
        self.ident=cache.hash_value(self.index);c.atomic_json(self.candidate/'index.json',self.index)
        self.entry,_=approval(self.ident,self.new_runtime);self.entry['producer']['id']=2;self.entry['artifact']['id']=4
        native={'schema':2,'runtime':self.new_runtime,'key':'3'*64,'compile_only':True,
            'inputs':{'Makefile':{'sha256':'4'*64,'mtime_ns':1}},'outputs':{}}
        for name,data in [('cgo/libmo.so',b'new native'),('cgo/.mo-native-provenance',b'new provenance')]:
            record=self.object(self.candidate,data);native['outputs'][name]={'sha256':record['output_id'],'size':record['size'],'mode':420,'mtime_ns':1}
        mid=cache.hash_value(native);c.atomic_json(self.candidate/'native'/(mid+'.json'),native)
        self.entry.update(native_manifests={'3'*64:mid},manifest_sha256='5'*64)
        c.atomic_json(self.candidate/'validated-bundle.json',{'index_sha256':self.ident,'manifest_sha256':'5'*64,'producer_id':2,'artifact_id':4})
        self.base=p.approval({**self.binding,'marker_sha256':c.digest(self.shared/'node-cache-identity.json')},[self.old_entry],self.entry)
        self.request=self.proof(self.entry)
        self.request['parent_authorizations']=[{'index_sha256':self.old_id,**self.proof(self.old_entry)}]
        self.before={str(v.relative_to(self.shared)):(v.stat().st_ino,c.digest(v)) for v in self.shared.rglob('*') if v.is_file()}
        for item in [patch.object(p,'cpu_profile',return_value=self.new_runtime['cpu']),
            patch.object(shutil,'disk_usage',return_value=shutil._ntuple_diskusage(100*c.GIB,0,100*c.GIB)),
            patch.dict(os.environ,{'CS_CACHE_ENV':'mty','ACTUAL_NODE':'mo-standard-1'})]:
            item.start();self.addCleanup(item.stop)

    def object(self,root,data):
        ident=hashlib.sha256(data).hexdigest()
        c.atomic_file(root/'objects'/ident[:2]/ident,io.BytesIO(data),len(data),ident,reserve=0)
        return {'output_id':ident,'size':len(data)}

    def proof(self,entry):
        producer=entry['producer']
        return {'binding':copy.deepcopy(self.binding),'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'run':{**producer,'repository':{'full_name':producer['repository']},'status':'completed','conclusion':'success',
                'actor':{'login':producer['actor']},'triggering_actor':{'login':producer['actor']}},
            'artifact':{**entry['artifact'],'expired':False,'workflow_run':{'id':producer['id'],'head_sha':producer['head_sha']}}}

    def unchanged(self):
        for name,value in self.before.items():self.assertEqual((self.shared/name).stat().st_ino,value[0]);self.assertEqual(c.digest(self.shared/name),value[1])

    def invoke(self):return p.append(self.candidate,self.shared,self.entry,self.base,self.request)

    def test_exact_different_cpu_generation_appends_without_old_bytes_or_marker_change(self):
        report=self.invoke();self.assertTrue(report['independently_verified']);self.assertFalse(report['compatibility_aliases'])
        self.assertEqual(report['compiler_invocations'],0);self.unchanged()
        self.assertEqual(report['existing_bytes'],len(b'old Go object'))
        self.assertEqual(report['new_bytes'],len(b'CPU-specific object')+len(b'new native')+len(b'new provenance'))
        # Same ActionID may be independently scoped to different CPU runtimes; never mix their lookups.
        old=c.Cache({'schema':2,'local':str(self.root/'old-private'),'shared':str(self.shared),'runtime':self.old_runtime,'indexes':[self.old_id],'reserve_bytes':0})
        new=c.Cache({'schema':2,'local':str(self.root/'new-private'),'shared':str(self.shared),'runtime':self.new_runtime,'indexes':[self.ident],'reserve_bytes':0})
        self.assertNotEqual(old.get('a'*64)['OutputID'],new.get('a'*64)['OutputID'])
        with self.assertRaisesRegex(ValueError,'runtime mismatch'):
            c.Cache({'schema':2,'local':str(self.root/'mixed-private'),'shared':str(self.shared),
                'runtime':self.old_runtime,'indexes':[self.old_id,self.ident],'reserve_bytes':0})

    def test_provenance_freshness_identity_cpu_and_object_negatives(self):
        for fault in ('stale-parent','parent-producer','expired-parent','candidate-producer','cpu','binding','parent-index','unknown-index','candidate-object','receipt'):
            # Each negative starts from a fresh tree and never changes protected old bytes.
            with self.subTest(fault=fault):
                test=Profiles('test_exact_different_cpu_generation_appends_without_old_bytes_or_marker_change');test.setUp()
                try:
                    if fault=='stale-parent':test.request['parent_authorizations'][0]['checked_at']='2000-01-01T00:00:00+00:00'
                    if fault=='parent-producer':test.request['parent_authorizations'][0]['run']['head_sha']='f'*40
                    if fault=='expired-parent':test.request['parent_authorizations'][0]['artifact']['expired']=True
                    if fault=='candidate-producer':test.request['run']['head_sha']='f'*40
                    if fault=='cpu':
                        with patch.object(p,'cpu_profile',return_value=CPU),self.assertRaises(ValueError):test.invoke()
                        test.unchanged();continue
                    if fault=='binding':test.request['binding']['pv_uid']='44444444-4444-4444-4444-444444444444'
                    if fault=='parent-index':test.base['existing_entries'][0]['index_sha256']='f'*64
                    if fault=='unknown-index':c.atomic_json(test.shared/'indexes'/('f'*64+'.json'),{'extra':True})
                    if fault=='candidate-object':
                        obj=next((test.candidate/'objects').glob('*/*'));obj.chmod(384);obj.write_bytes(b'corrupt')
                    if fault=='receipt':
                        path=test.candidate/'validated-bundle.json';path.chmod(384);path.write_bytes(c.canonical({'index_sha256':'0'*64}))
                    with self.assertRaises((ValueError,KeyError)):test.invoke()
                    test.unchanged();self.assertFalse((test.shared/'indexes'/(test.ident+'.json')).exists())
                finally:test.doCleanups()

    def test_non_cpu_contract_cannot_be_silently_changed(self):
        self.entry['runtime']={**self.entry['runtime'],'go':'other compiler'}
        self.base['candidate_runtime']=self.entry['runtime']
        with self.assertRaisesRegex(ValueError,'non-CPU'):self.invoke()
        self.unchanged()

    def test_base_rehashed_and_reauthorized_under_lock_before_writes(self):
        real=p.verify_base;calls=[]
        def checked(*a,**kw):
            calls.append(1)
            if len(calls)==2:raise ValueError('base changed under lock')
            return real(*a,**kw)
        with patch.object(p,'verify_base',side_effect=checked),self.assertRaisesRegex(ValueError,'under lock'):self.invoke()
        self.assertEqual(len(calls),2);self.unchanged();self.assertFalse((self.shared/'indexes'/(self.ident+'.json')).exists())

    def test_new_profile_requires_its_own_native_generation(self):
        self.entry['native_manifests']={}
        with self.assertRaisesRegex(ValueError,'lacks native'):self.invoke()
        self.unchanged()

    def test_parent_corruption_is_not_implicitly_repaired_or_overwritten(self):
        path=next((self.shared/'objects').glob('*/*'));path.chmod(384);path.write_bytes(b'corrupt old data')
        with self.assertRaisesRegex(ValueError,'base object corrupt'):self.invoke()
        self.assertEqual(path.read_bytes(),b'corrupt old data')
        self.assertFalse((self.shared/'indexes'/(self.ident+'.json')).exists())

    def test_unknown_native_metadata_or_mutated_candidate_native_refused(self):
        path=self.shared/'native'/('f'*64+'.json');c.atomic_json(path,{'unapproved':True})
        with self.assertRaisesRegex(ValueError,'metadata inventory'):self.invoke()
        self.unchanged();self.assertFalse((self.shared/'indexes'/(self.ident+'.json')).exists())

    def test_changed_candidate_native_bytes_never_publish(self):
        path=next((self.candidate/'native').glob('*.json'));path.chmod(384)
        native=c.read_json(path);native['runtime']=self.old_runtime;path.write_bytes(c.canonical(native))
        with self.assertRaisesRegex(ValueError,'native candidate not approved'):self.invoke()
        self.unchanged();self.assertFalse((self.shared/'indexes'/(self.ident+'.json')).exists())


if __name__=='__main__':unittest.main()
