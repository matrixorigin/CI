import hashlib
import io
import unittest
import zipfile

import incremental_cache as c
import node_cache_native_transport as native
import node_cache_transport as legacy
from test_node_cache_native_refresh import NativeOnly
from test_node_compile_cache import approval


class NativeTransport(unittest.TestCase):
    def setUp(self):
        self.fixture=NativeOnly('test_actual_native_export_bundle_stages_and_excludes_all_go_actions')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root;self.bundle,self.manifest=self.fixture.candidate()
        self.entry,self.api=approval(self.manifest['index_sha256'],self.fixture.runtime)
        self.entry.update(native_only=True,source=self.fixture.sha,native_manifests=self.manifest['native_manifests'],
            manifest_sha256=c.digest(self.bundle/'manifest.json'))
        self.raw=self.zip()

    def zip(self,extra=None):
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as archive:
            for name in ('manifest.json','delta.tar.gz'):archive.writestr(name,(self.bundle/name).read_bytes())
            if extra:archive.writestr(*extra)
        value=raw.getvalue();self.entry['artifact'].update(size_in_bytes=len(value),digest='sha256:'+hashlib.sha256(value).hexdigest())
        return value

    def receive(self,receiver=native.receive):
        raw=self.raw
        class Response(io.BytesIO):
            status=200
            headers={'Content-Length':str(len(raw))}
        return receiver(self.entry,self.root/'received',self.root/'staged',self.api,
            url_factory=lambda _: 'https://fixture.invalid/signed',open_url=lambda *args,**kwargs:Response(raw))

    def test_native_exact_two_member_zip_authenticates_index_inside_full_verified_tar(self):
        path=self.receive();self.assertEqual(c.digest(path),self.entry['index_sha256'])
        self.assertEqual(c.read_json(path)['actions'],{})
        self.assertTrue((self.root/'staged/validated-bundle.json').is_file())
        self.assertEqual(list(self.fixture.shared.iterdir()),[])

    def test_old_three_member_contract_not_relaxed(self):
        with self.assertRaises(ValueError):self.receive(legacy.receive)
        self.assertFalse((self.root/'staged').exists())

    def test_native_format_never_falls_back_for_unapproved_or_corrupt_bytes(self):
        for fault in ('not-native','manifest','digest','source','extra','index'):
            fixture=NativeTransport('test_native_exact_two_member_zip_authenticates_index_inside_full_verified_tar')
            fixture.setUp()
            try:
                if fault=='not-native':fixture.entry['native_only']=False
                if fault=='manifest':fixture.entry['manifest_sha256']='0'*64
                if fault=='digest':fixture.entry['artifact']['digest']='sha256:'+'0'*64
                if fault=='source':fixture.entry['source']='0'*40
                if fault=='extra':fixture.raw=fixture.zip(('../other',b'not allowed'))
                if fault=='index':fixture.entry['index_sha256']='0'*64
                with self.subTest(fault=fault),self.assertRaises(ValueError):fixture.receive()
                self.assertFalse((fixture.root/'staged').exists())
                self.assertEqual(list(fixture.fixture.shared.iterdir()),[])
            finally:fixture.doCleanups()


if __name__=='__main__':unittest.main()
