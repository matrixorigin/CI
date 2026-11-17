import hashlib
import io
import zipfile
import unittest
from unittest.mock import patch

import incremental_cache as c
import node_cache_transport as t
from test_node_cache_candidate import CandidateTests


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture=CandidateTests('test_frozen_delta_excludes_parent_and_tests_cannot_mutate_snapshot')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.manifest,self.entry,self.api=self.fixture.frozen()
        self.root=self.fixture.root
        self.raw=self.zip()

    def zip(self, extra=None):
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as z:
            for name in ('manifest.json','index.json','delta.tar.gz'):
                z.writestr(name,(self.fixture.stage/name).read_bytes())
            if extra:z.writestr(*extra)
        result=raw.getvalue()
        self.entry['artifact'].update(size_in_bytes=len(result),digest='sha256:'+hashlib.sha256(result).hexdigest())
        return result

    def receive(self, raw=None, length=None):
        data=self.raw if raw is None else raw
        class Response(io.BytesIO):
            status=200
            headers={'Content-Length':str(len(data) if length is None else length)}
        return t.receive(self.entry,self.root/'received',self.root/'validated',self.api,
                         url_factory=lambda _: 'https://fixture.invalid/signed',
                         open_url=lambda *args,**kwargs: Response(data))

    def test_full_transport_manifest_archive_and_objects_pass(self):
        index=self.receive()
        self.assertEqual(c.digest(index),self.entry['index_sha256'])
        self.assertTrue((self.root/'validated/validated-bundle.json').is_file())
        self.assertFalse((self.root/'received/transport.zip.partial').exists())

    def test_bad_digest_retains_partial_and_no_stage(self):
        self.entry['artifact']['digest']='sha256:'+'0'*64
        with self.assertRaises(ValueError):self.receive()
        self.assertTrue((self.root/'received/transport.zip.partial').exists())
        self.assertFalse((self.root/'validated').exists())

    def test_length_mismatch_refuses(self):
        with self.assertRaises(ValueError):self.receive(length=len(self.raw)+1)
        self.assertFalse((self.root/'validated').exists())

    def test_unexpected_member_refuses_even_with_matching_transport_hash(self):
        self.raw=self.zip(('../outside',b'not allowed'))
        with self.assertRaises(ValueError):self.receive()
        self.assertFalse((self.root/'outside').exists())
        self.assertFalse((self.root/'validated').exists())

    def test_approved_manifest_digest_is_mandatory(self):
        self.entry['manifest_sha256']='0'*64
        with self.assertRaises(ValueError):self.receive()
        self.assertFalse((self.root/'validated').exists())

    def test_existing_stage_never_overwritten(self):
        (self.root/'received').mkdir()
        (self.root/'received/old-partial').write_bytes(b'preserve')
        with self.assertRaises(ValueError):self.receive()
        self.assertEqual((self.root/'received/old-partial').read_bytes(),b'preserve')

    def test_expired_approval_stops_before_transfer(self):
        self.entry['expires_at']='2000-01-01T00:00:00Z'
        with self.assertRaises(ValueError):self.receive()
        self.assertFalse((self.root/'received').exists())


if __name__=='__main__':unittest.main()
