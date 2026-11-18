import copy
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
import sys
from unittest.mock import patch

import incremental_cache as c
import node_cache_replication as r
import node_cache_toolchain as t


class Replication(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.source_root = self.root/'source'; self.source_root.mkdir()
        environment=getattr(self,'environment','mty')
        self.source = r.bound_identity({'environment':environment,'node':'mo-standard-0',
            'node_uid':'00000000-0000-4000-8000-000000000001',
            'pvc_uid':'00000000-0000-4000-8000-000000000002',
            'pv_uid':'00000000-0000-4000-8000-000000000003'})
        runtime = {'environment':environment, 'tool_binaries':{}, 'schema':2}
        payload = b'existing reviewed object'; digest = r.sha(payload)
        self.object_name = 'incremental-v2/objects/'+digest[:2]+'/'+digest
        obj = self.source_root/self.object_name; obj.parent.mkdir(parents=True); obj.write_bytes(payload)
        index = {'schema':2, 'runtime':runtime, 'actions':{'a'*64:{'output_id':digest,'size':len(payload)}}}
        ident = r.sha(c.canonical(index)); c.atomic_json(self.source_root/'incremental-v2/indexes'/(ident+'.json'), index)
        c.atomic_json(self.source_root/'incremental-v2/node-cache-identity.json',
                      {k:v for k,v in self.source.items() if k!='marker_sha256'})
        self.catalog = {'schema':2,'nodes':[self.source], 'entries':[{'compile_only':True,
            'runtime':runtime,'index_sha256':ident,'native_manifests':{},
            'bindings':[{k:self.source[k] for k in ('node_uid','pvc_uid','pv_uid')}]}]}
        raw = b'previously reviewed tool archive'; toolsha = r.sha(raw)
        self.lock = {'schema':1,'versions':t.VERSIONS,'archive_sha256':toolsha,'archive_size':len(raw),
            'producer':{'repository':'matrixorigin/CI','event':'push','attempt':1,
                        'workflow_path':'.github/workflows/node-cache-canary.yaml','sha':'a'*40,'id':42},
            'artifact':{'id':9,'name':'reviewed-toolchain-contract-42','size':123,'digest':'sha256:'+'b'*64}}
        archive = self.source_root/'toolchains-v1'/toolsha/'toolchain.tar.gz'
        archive.parent.mkdir(parents=True); archive.write_bytes(raw)
        c.atomic_json(archive.parent/'receipt.json', {'schema':1,'lock':self.lock,'binding':self.source})
        self.space = patch.object(r.shutil,'disk_usage',return_value=type('Space',(),{'free':100*c.GIB})())
        self.space.start(); self.addCleanup(self.space.stop)

    def target(self, node=1, env='mty'):
        return {'environment':env,'node':f'mo-standard-{node}',
            'node_uid':f'10000000-0000-4000-8000-00000000000{node}',
            'pvc_uid':f'20000000-0000-4000-8000-00000000000{node}',
            'pv_uid':f'30000000-0000-4000-8000-00000000000{node}'}

    def bundle(self):
        out = io.BytesIO(); r.export(self.source_root,self.catalog,self.source,self.lock,out)
        return out.getvalue()

    def receive(self, data=None, target=None, name='target'):
        destination = self.root/name; destination.mkdir()
        result = r.receive(destination,self.catalog,self.source,self.lock,target or self.target(),
                           io.BytesIO(self.bundle() if data is None else data))
        return destination,result

    def rewrite(self, change):
        out=io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(self.bundle())) as source, tarfile.open(fileobj=out,mode='w') as dest:
            rows=[(x,source.extractfile(x).read()) for x in source]
            for info,raw in change(rows):
                info=copy.copy(info);info.size=len(raw);dest.addfile(info,io.BytesIO(raw))
        return out.getvalue()

    def test_two_nodes_same_objects_new_bindings_source_unchanged(self):
        before=self.bundle()
        for node in (1,2):
            target=self.target(node);destination,result=self.receive(target=target,name=f'target{node}')
            self.assertTrue(result['success']);self.assertEqual(result['compiler_invocations'],0)
            self.assertEqual(result['external_downloads'],0)
            self.assertEqual(result['binding'],r.bound_identity(target))
            plan=r.make_plan(self.source_root,self.catalog,self.source,self.lock)
            self.assertTrue(r.verify(destination,plan,self.catalog,self.source,self.lock,target)['success'])
        self.assertEqual(before,self.bundle())

    def test_existing_destination_refused_without_overwrite(self):
        destination,result=self.receive();before=c.digest(destination/self.object_name)
        with self.assertRaises(ValueError):
            r.receive(destination,self.catalog,self.source,self.lock,self.target(),io.BytesIO(self.bundle()))
        self.assertEqual(before,c.digest(destination/self.object_name))

    def test_cross_environment_and_source_node_refused(self):
        for target in (self.target(env='ruh'),{k:v for k,v in self.source.items() if k!='marker_sha256'}):
            destination=self.root/target['environment'];destination.mkdir(exist_ok=True)
            with self.assertRaises(ValueError):r.receive(destination,self.catalog,self.source,self.lock,target,io.BytesIO(self.bundle()))
            self.assertFalse((destination/'incremental-v2').exists())

    def test_corrupt_object_and_missing_object_never_publish(self):
        def corrupt(rows):
            return [(i,b'X'*len(raw) if i.name==self.object_name else raw) for i,raw in rows]
        for number,change in enumerate((corrupt,lambda rows:[x for x in rows if x[0].name!=self.object_name])):
            destination=self.root/f'bad{number}';destination.mkdir()
            with self.assertRaises(ValueError):
                r.receive(destination,self.catalog,self.source,self.lock,self.target(),io.BytesIO(self.rewrite(change)))
            self.assertFalse((destination/'incremental-v2').exists())

    def test_extra_member_and_duplicate_and_symlink_rejected(self):
        def traversal(rows):
            info=tarfile.TarInfo('../escape');return rows+[(info,b'x')]
        def link(rows):
            info=tarfile.TarInfo('unexpected');info.type=tarfile.SYMTYPE;info.linkname='/tmp/other'
            return rows+[(info,b'')]
        for number,change in enumerate((traversal,lambda rows:rows+[rows[-1]],link)):
            destination=self.root/f'extra{number}';destination.mkdir()
            with self.assertRaises(ValueError):
                r.receive(destination,self.catalog,self.source,self.lock,self.target(),io.BytesIO(self.rewrite(change)))
            self.assertFalse((destination/'incremental-v2').exists())

    def test_independent_checksum_and_source_corruption_rejected(self):
        destination,result=self.receive();(destination/self.object_name).chmod(0o644);(destination/self.object_name).write_bytes(b'bad')
        plan=r.make_plan(self.source_root,self.catalog,self.source,self.lock)
        with self.assertRaises(ValueError):r.verify(destination,plan,self.catalog,self.source,self.lock,self.target())
        (self.source_root/self.object_name).write_bytes(b'bad')
        with self.assertRaises(ValueError):self.bundle()

    def test_disk_reserve_fails_before_staging(self):
        bundle=self.bundle();destination=self.root/'small';destination.mkdir()
        with patch.object(r.shutil,'disk_usage',return_value=type('Space',(),{'free':20*c.GIB})()):
            with self.assertRaises(ValueError):r.receive(destination,self.catalog,self.source,self.lock,self.target(),io.BytesIO(bundle))
        self.assertFalse((destination/'.replication-stage').exists())

    def test_separate_cli_verifier_and_projected_configmap_inputs(self):
        destination,result=self.receive();version=self.root/'..version';version.mkdir()
        for name,value in [('catalog',self.catalog),('lock',self.lock),('source',self.source)]:
            path=version/(name+'.json');path.write_text(json.dumps(value))
            (self.root/(name+'.json')).symlink_to(path)
        argv=['replicate','verify','--shared',str(destination),'--target-json',json.dumps(self.target())]
        for name in ('catalog','lock','source'):argv+=['--'+name,str(self.root/(name+'.json'))]
        out=io.StringIO()
        with patch.object(sys,'argv',argv),redirect_stdout(out):r.main()
        self.assertTrue(json.loads(out.getvalue())['independently_verified'])


if __name__=='__main__':unittest.main()
