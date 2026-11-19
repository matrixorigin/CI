import hashlib
from pathlib import Path
import tempfile
import unittest
import node_cache_native_missing_plan as p


class MissingOnly(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name).resolve()
        self.a=hashlib.sha256(b'a').hexdigest();self.b=hashlib.sha256(b'bb').hexdigest()
        self.man={'key':'0'*64,'runtime':{},'compile_only':True,'outputs':{
            'cgo/a.o':{'sha256':self.a,'size':1},'cgo/c.o':{'sha256':self.a,'size':1},
            'cgo/b.o':{'sha256':self.b,'size':2}}}
        self.entry={'native_only':True,'native_manifests':{'0'*64:p.cache.hash_value(self.man)},'runtime':{}}
        self.dest=self.root/'objects'/self.a[:2]/self.a;self.dest.parent.mkdir(parents=True)

    def result(self):return p.plan(self.root,self.entry,self.man)

    def test_missing_then_partial_then_fully_warm(self):
        self.assertEqual(self.result()['payload_bytes'],3)
        self.dest.write_bytes(b'a')
        self.assertEqual(self.result(),{'missing':{self.b:2},'present':{self.a:1},'total_objects':2,'payload_bytes':2,'reused_bytes':1})
        b=self.root/'objects'/self.b[:2]/self.b;b.parent.mkdir(exist_ok=True);b.write_bytes(b'bb')
        self.assertEqual(self.result()['payload_bytes'],0)

    def test_corrupt_preserved(self):
        self.dest.write_bytes(b'x')
        with self.assertRaises(ValueError):self.result()
        self.assertEqual(self.dest.read_bytes(),b'x')

    def test_symlink_rejected(self):
        self.dest.symlink_to(self.root/'absent')
        with self.assertRaises(ValueError):self.result()

    def test_modified_inventory_rejected(self):
        self.man['outputs'].pop('cgo/b.o')
        with self.assertRaises(ValueError):self.result()
