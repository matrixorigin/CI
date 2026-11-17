import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import incremental_cache as c
import node_compile_cache as n
import publish_node_cache as p


def approval(index,runtime):
    producer={'id':1,'head_sha':'a'*40,'head_branch':'trusted','event':'workflow_dispatch',
              'path':'.github/workflows/trusted.yml','workflow_id':2,'run_attempt':1,
              'repository':'matrixorigin/matrixone','actor':'approved',
              'referenced_workflows':[{'path':'matrixorigin/CI/.github/workflows/trusted.yml','sha':'b'*40}]}
    artifact={'id':3,'name':'compile-only','size_in_bytes':123,'digest':'sha256:'+'c'*64}
    entry={'index_sha256':index,'runtime':runtime,'expires_at':'2099-01-01T00:00:00Z',
           'compile_only':True,'producer':producer,'artifact':artifact}
    def api(path):
        if '/runs/' in path:
            return {**producer,'repository':{'full_name':producer['repository']},'status':'completed','conclusion':'success',
                    'actor':{'login':'approved'},'triggering_actor':{'login':'approved'}}
        return {**artifact,'expired':False,'workflow_run':{'id':1,'head_sha':'a'*40}}
    return entry,api


class NodeCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.runtime={'environment':'mty','go':'fixture'}

    def test_node_binding_is_not_group_or_runner_name(self):
        binding={'environment':'mty','node':'node-0','node_uid':'node-uid','pvc_uid':'pvc-uid','pv_uid':'pv-uid','namespace_uid':'ns-uid'}
        shared=self.root/'shared';shared.mkdir()
        c.atomic_json(shared/'node-cache-identity.json',binding)
        catalog={'schema':2,'nodes':[{**binding,'marker_sha256':c.digest(shared/'node-cache-identity.json')}]}
        hints={k:binding[k] for k in ('node_uid','pvc_uid','pv_uid')}
        n.verify_binding(catalog,shared,'mty','node-0',hints)
        with self.assertRaises(ValueError):n.verify_binding(catalog,shared,'jed','node-0',hints)
        with self.assertRaises(ValueError):n.verify_binding(catalog,shared,'mty','node-0',{**hints,'pv_uid':'other'})

    def test_compatibility_expiry_unavailable_are_miss_but_provenance_is_fatal(self):
        entry,api=approval('d'*64,self.runtime)
        self.assertEqual(n.reviewed_entries({'entries':[entry]},self.runtime,api),[entry])
        self.assertEqual(n.reviewed_entries({'entries':[entry]},{**self.runtime,'go':'other'},api),[])
        self.assertEqual(n.reviewed_entries({'entries':[{**entry,'expires_at':'2000-01-01T00:00:00Z'}]},self.runtime,api),[])
        def unavailable(_):raise PermissionError()
        self.assertEqual(n.reviewed_entries({'entries':[entry]},self.runtime,unavailable),[])
        def bad(path):
            value=api(path)
            if '/runs/' in path:value['head_sha']='f'*40
            return value
        with self.assertRaises(ValueError):n.reviewed_entries({'entries':[entry]},self.runtime,bad)
        with self.assertRaises(ValueError):n.reviewed_entries({'entries':[{**entry,'compile_only':False}]},self.runtime,api)

    def test_delta_publication_keeps_existing_generation_and_copies_only_new_bytes(self):
        local=self.root/'private';shared=self.root/'shared'
        cache=c.Cache({'schema':2,'local':str(local),'shared':None,'indexes':[],'runtime':self.runtime,'reserve_bytes':0})
        data=b'unchanged object';sha=hashlib.sha256(data).hexdigest()
        cache.put('a'*64,sha,len(data),io.BytesIO(data))
        first=self.root/'first.json';index=p.export_delta(local,self.runtime,{},first)
        entry,api=approval(index,self.runtime)
        report=p.publish(local,first,shared,entry,api)
        self.assertEqual(report['new_bytes'],len(data))
        again=p.publish(local,first,shared,entry,api)
        self.assertEqual(again['new_bytes'],0)
        original=c.digest(shared/'indexes'/(index+'.json'))
        cache.put('b'*64,hashlib.sha256(b'new').hexdigest(),3,io.BytesIO(b'new'))
        second=self.root/'second.json';parents=c.read_json(first)['actions']
        index2=p.export_delta(local,self.runtime,parents,second)
        self.assertEqual(set(c.read_json(second)['actions']),{'b'*64})
        entry2,api2=approval(index2,self.runtime)
        report2=p.publish(local,second,shared,entry2,api2)
        self.assertEqual(report2['new_bytes'],3)
        self.assertEqual(c.digest(shared/'indexes'/(index+'.json')),original)

    def test_unapproved_or_corrupted_candidate_cannot_publish(self):
        local=self.root/'private';local.mkdir()
        index=self.root/'index.json';c.atomic_json(index,{'schema':2,'runtime':self.runtime,'actions':{}})
        entry,api=approval('f'*64,self.runtime)
        with self.assertRaises(ValueError):p.publish(local,index,self.root/'shared',entry,api)
        self.assertFalse((self.root/'shared').exists())

    def test_native_key_ignores_go_edit_but_tracks_native_content_and_build_flags(self):
        source=self.root/'source';(source/'cgo').mkdir(parents=True)
        (source/'Makefile').write_text('all:\n')
        (source/'cgo/lib.c').write_text('int value=1;')
        (source/'main.go').write_text('package main')
        with patch.object(n.legacy,'native_inputs',return_value=['Makefile','cgo/lib.c']):
            first=n.native_key(source,self.runtime)[0]
            (source/'main.go').write_text('package main // new SHA')
            self.assertEqual(n.native_key(source,self.runtime)[0],first)
            (source/'cgo/lib.c').write_text('int value=2;')
            second=n.native_key(source,self.runtime)[0];self.assertNotEqual(first,second)
            with patch.dict(os.environ,{'CFLAGS':'-O0'}):self.assertNotEqual(n.native_key(source,self.runtime)[0],second)

    def test_error_marker_overrides_apparent_test_success(self):
        local=self.root/'private';local.mkdir()
        c.atomic_json(local/'validation-failed.json',{'invalid':True})
        state=self.root/'state.json';c.atomic_json(state,{'config':{'local':str(local)},'configured_seconds':0})
        with self.assertRaises(ValueError):n.acceptance(state)

    def test_one_time_disk_cache_conversion_never_compiles_and_rejects_unknown_format(self):
        disk=self.root/'old-go-cache';local=self.root/'private'
        data=b'audited compiled object';output=hashlib.sha256(data).hexdigest();action='a'*64
        (disk/action[:2]).mkdir(parents=True);(disk/output[:2]).mkdir(parents=True,exist_ok=True)
        (disk/output[:2]/(output+'-d')).write_bytes(data)
        header=f'v1 {action} {output} {len(data):20d} {1:20d}\n'
        (disk/action[:2]/(action+'-a')).write_text(header)
        candidate=self.root/'candidate.json'
        with patch('subprocess.run',side_effect=AssertionError('conversion must not compile')):
            p.import_reviewed_disk_cache(disk,local,self.runtime,candidate)
        self.assertEqual(c.read_json(candidate)['actions'][action]['output_id'],output)
        (disk/action[:2]/(action+'-a')).write_text(header.replace('v1','v2'))
        with self.assertRaises(ValueError):p.import_reviewed_disk_cache(disk,self.root/'other',self.runtime,self.root/'bad.json')


if __name__=='__main__':unittest.main()
