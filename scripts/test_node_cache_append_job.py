import unittest
from unittest.mock import patch

import incremental_cache as c
import node_cache_append_job as j
import test_node_cache_append as fixture


class AppendJobTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.AppendTests('test_append_new_objects_without_recompilation_or_old_mutation')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.stage=self.f.f.stage;self.entry=self.f.f.entry;self.request=self.f.f.request
        self.base=self.f.base;self.shared=self.f.shared
        for patcher in (patch.object(j,'STAGE',self.stage),patch.object(j,'SHARED',self.shared)):
            patcher.start();self.addCleanup(patcher.stop)
        # Existing fixture stage is authenticated; add the entrypoint receipt.
        (self.stage/'stage-receipt.json').unlink()
        c.atomic_json(self.stage/'stage-receipt.json',{'success':True,'index_sha256':self.entry['index_sha256']})

    def invoke(self,phase):return j.execute(phase,self.entry,self.base,self.request)

    def test_append_and_separate_verify_preserve_base(self):
        self.assertEqual(self.invoke('append')['compiler_invocations'],0)
        self.assertTrue(self.invoke('verify')['independently_verified'])
        self.f.assert_old_unchanged()

    def test_stage_download_only_after_base_checks(self):
        empty=self.stage/'transport-stage';empty.mkdir()
        self.request['url']='https://fixture.blob.core.windows.net/object'
        with patch.object(j,'STAGE',empty),patch.object(j.transport,'receive',return_value=self.stage/'candidate/index.json') as receive:
            self.assertFalse(self.invoke('stage')['shared_cache_modified'])
            self.assertEqual(receive.call_count,1)
        self.f.assert_old_unchanged()

    def test_changed_marker_prevents_download(self):
        self.base['marker_sha256']='0'*64
        self.request['url']='https://fixture.blob.core.windows.net/object'
        with patch.object(j.transport,'receive') as receive:
            with self.assertRaises(ValueError):self.invoke('stage')
            receive.assert_not_called()

    def test_verify_requires_complete_append_receipt(self):
        with self.assertRaises((ValueError,FileNotFoundError)):self.invoke('verify')
        self.f.assert_old_unchanged()

    def test_verify_rehashes_old_objects(self):
        self.invoke('append')
        obj=self.shared/'objects'/self.f.f.f.old_id[:2]/self.f.f.f.old_id
        obj.chmod(0o600);obj.write_bytes(b'changed-after-publish')
        with self.assertRaises(ValueError):self.invoke('verify')


if __name__=='__main__':unittest.main()
