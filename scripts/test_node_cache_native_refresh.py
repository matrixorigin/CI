import copy
import hashlib
import io
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import incremental_cache as c
import node_cache_native_refresh as refresh
import node_cache_bundle as bundle
from test_node_compile_cache import approval


class NativeOnly(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.source=self.root/'source';self.source.mkdir()
        self.local=self.root/'private';self.shared=self.root/'shared';self.shared.mkdir()
        (self.source/'Makefile').write_text('fixture native build')
        (self.source/'cgo').mkdir();(self.source/'cgo/libmo.so').write_bytes(b'new exact native output')
        (self.source/'cgo/.mo-native-provenance').write_bytes(b'original Make provenance')
        self.runtime={'environment':'mty','repository':'matrixorigin/matrixone','cpu':{'fixture':'cpu0'}}
        self.inputs={'Makefile':c.digest(self.source/'Makefile')};self.key='b'*64;self.sha='a'*40
        self.binding={'environment':'mty','node':'mo-standard-0','node_uid':'node-fixture','pvc_uid':'pvc-fixture','pv_uid':'pv-fixture'}
        self.state={'config':{'schema':2,'runtime':self.runtime,'shared':str(self.shared),
            'local':str(self.local),'indexes':['c'*64]},'native_key':self.key,'native_inputs':self.inputs,
            'entries':[{'fixture':'parent'}],'source':str(self.source)}
        profile={'runtime':self.runtime,'native_key':self.key}
        self.request={'schema':1,'environment':'mty','source_repository':'matrixorigin/matrixone',
            'source_sha':self.sha,'current_pr':32871,'current_pr_head_sha':'d'*40,
            'native_inputs_equal_current_pr':True,'native_input_count':1,'profiles':{'mo-standard-0':profile,'mo-standard-1':profile},
            'tool_archive_sha256':'e'*64,'tool_archive_bytes':123}
        self.tools={'binding':self.binding,'persistent_tools':True,'external_tool_downloads':0,
            'archive_sha256':'e'*64,'archive_bytes':123}
        for item in (patch.dict(os.environ,{'CS_CACHE_NODE':'mo-standard-0','CS_CACHE_EXPECTED_NODE':'mo-standard-0',
                'CS_CACHE_NODE_UID':'node-fixture','CS_CACHE_PVC_UID':'pvc-fixture','CS_CACHE_PV_UID':'pv-fixture'}),
            patch.object(refresh.n,'verify_binding',return_value=self.binding),
            patch.object(refresh.n,'native_manifest',return_value=None),
            patch.object(refresh.n,'runtime',return_value=self.runtime),
            patch.object(refresh.n,'command',return_value=self.sha),
            patch.object(refresh.n,'native_key',return_value=(self.key,self.inputs)),
            patch.object(shutil,'disk_usage',return_value=shutil._ntuple_diskusage(100*c.GIB,0,100*c.GIB))):
            item.start();self.addCleanup(item.stop)
        self.state_path=self.root/'state.json';c.atomic_json(self.state_path,self.state)
        c.atomic_json(self.root/'native-result.json',{'native_hit':False,'native_seconds':1.25,
            'native_output_sha256':refresh.n.native_digest(self.source)})

    def preflight(self,state=None,request=None,tools=None):
        return refresh.preflight(state or self.state,request or self.request,tools or self.tools,{},self.sha,'mo-standard-0')

    def test_observed_exact_native_key_and_existing_readonly_generation_required(self):
        result=self.preflight();self.assertTrue(result['native_only']);self.assertFalse(result['go_precompile'])
        self.assertEqual(result['shared_cache_writes'],0);self.assertFalse(result['ut_execution'])
        for fault in ('source','pr','scope','key','runtime','parents','tools','binding','archive'):
            state,request,tools=copy.deepcopy((self.state,self.request,self.tools))
            if fault=='source':request['source_repository']='fixture/fork'
            if fault=='pr':request['native_inputs_equal_current_pr']=False
            if fault=='scope':request['profiles'].pop('mo-standard-1')
            if fault=='key':request['profiles']['mo-standard-0']['native_key']='f'*64
            if fault=='runtime':state['config']['runtime']={'different':'CPU'}
            if fault=='parents':state['config']['indexes']=[]
            if fault=='tools':tools['external_tool_downloads']=1
            if fault=='binding':tools['binding']={}
            if fault=='archive':tools['archive_sha256']='f'*64
            with self.subTest(fault=fault),self.assertRaises(ValueError):self.preflight(state,request,tools)
        with patch.object(refresh.n,'native_manifest',return_value={'already':'published'}),self.assertRaises(ValueError):self.preflight()

    def candidate(self):
        out=self.root/'candidate'
        # A normal Go action may exist privately. It MUST NOT enter this bundle.
        c.atomic_json(self.local/'actions'/('f'*2)/('f'*64+'.json'),{'private':'Go object'})
        manifest=refresh.freeze(self.state_path,self.request,out)
        return out,manifest

    def test_actual_native_export_bundle_stages_and_excludes_all_go_actions(self):
        out,value=self.candidate();self.assertTrue(value['native_only']);self.assertFalse(value['go_precompile'])
        self.assertEqual(value['object_count'],2);self.assertEqual(c.read_json(out/'index.json')['actions'],{})
        with tarfile.open(out/'delta.tar.gz') as archive:
            self.assertEqual(len(archive.getnames()),4)
            self.assertFalse(any('actions/' in name or 'gomod' in name for name in archive.getnames()))
        entry,_=approval(value['index_sha256'],self.runtime)
        entry.update(native_manifests=value['native_manifests'],manifest_sha256=c.digest(out/'manifest.json'))
        transport=out/'transport.zip';transport.write_bytes(b'independently authenticated fixture transport')
        entry['artifact'].update(size_in_bytes=transport.stat().st_size,digest='sha256:'+c.digest(transport))
        def api(path):
            producer=entry['producer']
            if '/runs/' in path:return {**producer,'repository':{'full_name':producer['repository']},
                'status':'completed','conclusion':'success','actor':{'login':producer['actor']},
                'triggering_actor':{'login':producer['actor']}}
            return {**entry['artifact'],'expired':False,'workflow_run':{'id':1,'head_sha':'a'*40}}
        staged=bundle.stage(out,entry,self.root/'staged',api)
        self.assertEqual(c.digest(staged),value['index_sha256'])
        self.assertEqual(list(self.shared.iterdir()),[])
        with self.assertRaises(ValueError):refresh.freeze(self.state_path,self.request,out)

    def test_source_input_or_runtime_movement_never_completes_candidate(self):
        for fault in ('source','runtime','inputs','bytes'):
            request=copy.deepcopy(self.request);out=self.root/('reject-'+fault)
            if fault=='source':request['source_sha']='0'*40
            if fault=='runtime':request['profiles']['mo-standard-0']['runtime']={'wrong':'ABI'}
            if fault=='inputs':request['profiles']['mo-standard-0']['native_key']='0'*64
            if fault=='bytes':
                with patch.object(refresh.n,'native_digest',return_value='0'*64),self.assertRaises(ValueError):refresh.freeze(self.state_path,request,out)
                continue
            with self.subTest(fault=fault),self.assertRaises(ValueError):refresh.freeze(self.state_path,request,out)
            self.assertFalse((out/'manifest.json').exists())


if __name__=='__main__':unittest.main()
