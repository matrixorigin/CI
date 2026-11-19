import copy
from pathlib import Path
from unittest.mock import patch
import unittest
import node_cache_pvc_scan as scan
from test_node_cache_replication import Replication


class PersistentScan(Replication):
    def setUp(self):
        super().setUp()
        p=patch.object(scan.cache,'readonly_mount');p.start();self.addCleanup(p.stop)

    def verify(self):return scan.verify(self.source_root,self.catalog,self.lock,self.source)

    def test_full_hashes_and_all_source_bytes_unchanged(self):
        before={str(p.relative_to(self.source_root)):p.read_bytes() for p in self.source_root.rglob('*') if p.is_file()}
        result=self.verify()
        self.assertTrue(result['success']);self.assertTrue(result['readonly'])
        self.assertEqual(result['filesystem_writes'],0);self.assertEqual(result['compiler_invocations'],0)
        self.assertEqual(result['external_downloads'],0);self.assertEqual(result['indexes'][0]['action_count'],1)
        self.assertEqual(before,{str(p.relative_to(self.source_root)):p.read_bytes() for p in self.source_root.rglob('*') if p.is_file()})

    def test_corrupt_or_missing_object_never_approves(self):
        path=self.source_root/self.object_name;raw=path.read_bytes();path.write_bytes(b'x'*len(raw))
        with self.assertRaises(ValueError):self.verify()
        path.unlink()
        with self.assertRaises(ValueError):self.verify()

    def test_changed_volume_or_tool_receipt_never_approves(self):
        wrong=copy.deepcopy(self.source);wrong['pvc_uid']='90000000-0000-4000-8000-000000000001'
        with self.assertRaises(ValueError):scan.verify(self.source_root,self.catalog,self.lock,wrong)
        (self.source_root/'toolchains-v1'/self.lock['archive_sha256']/'receipt.json').unlink()
        with self.assertRaises(FileNotFoundError):self.verify()


if __name__=='__main__':unittest.main()
