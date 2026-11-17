import copy
import unittest
import tempfile
from pathlib import Path
import node_compile_cache as n

class Authority(unittest.TestCase):
    def setUp(self):
        self.binding={'environment':'mty','node':'mo-standard-0','node_uid':'node','pvc_uid':'pvc','pv_uid':'pv'}
        self.key='a'*64;self.mid='b'*64;self.index='c'*64
        self.entry={'index_sha256':self.index,'native_manifests':{self.key:self.mid},
                    'bindings':[{k:self.binding[k] for k in ('node_uid','pvc_uid','pv_uid')}]}
        self.row={**self.binding,'native_key':self.key,'manifest_sha256':self.mid,'index_sha256':self.index}
        self.catalog={'entries':[self.entry],'native_authorities':[self.row]}

    def test_only_reviewed_published_active_manifest_is_selected(self):
        before=copy.deepcopy(self.catalog)
        self.assertEqual(n.native_authorities(self.catalog,self.binding,[self.entry]),{self.key:self.mid})
        self.assertEqual(self.catalog,before)

    def test_expired_or_unavailable_native_authority_is_cold_miss(self):
        self.assertEqual(n.native_authorities(self.catalog,self.binding,[]),{self.key:None})

    def test_other_volume_authority_not_used(self):
        self.assertEqual(n.native_authorities(self.catalog,{**self.binding,'pvc_uid':'different'},[self.entry]),{})

    def test_unpublished_unknown_or_duplicate_authority_rejected(self):
        for case in ('unpublished','unknown','duplicate','extra','bad_hash'):
            with self.subTest(case=case):
                catalog=copy.deepcopy(self.catalog)
                if case=='unpublished':catalog['entries'][0]['bindings']=[]
                if case=='unknown':catalog['native_authorities'][0]['manifest_sha256']='d'*64
                if case=='duplicate':catalog['native_authorities'].append(dict(self.row))
                if case=='extra':catalog['native_authorities'][0]['ignore_hashes']=True
                if case=='bad_hash':catalog['native_authorities'][0]['native_key']='invalid'
                with self.assertRaises(ValueError):n.native_authorities(catalog,self.binding,[self.entry])

    def test_explicit_selection_still_checks_full_manifest_hash_and_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'native').mkdir()
            manifest={'schema':2,'key':self.key,'runtime':{'go':'fixture'},'inputs':{},
                      'outputs':{'cgo/libmo.so':{}},'compile_only':True}
            content=n.c.canonical(manifest);mid=n.c.hashlib.sha256(content).hexdigest()
            path=root/'native'/(mid+'.json');path.write_bytes(content)
            state={'native_key':self.key,'native_inputs':{},'config':{'shared':str(root),'runtime':{'go':'fixture'}},
                   'entries':[{'native_manifests':{self.key:mid}},{'native_manifests':{self.key:'d'*64}}]}
            with self.assertRaises(ValueError):n.native_manifest(state)
            state['native_authorities']={self.key:mid}
            self.assertEqual(n.native_manifest(state),manifest)
            path.write_bytes(content+b' ')
            with self.assertRaises(ValueError):n.native_manifest(state)

if __name__=='__main__':unittest.main()
