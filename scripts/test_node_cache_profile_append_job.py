import copy
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import node_cache_profile_append_job as j
import test_node_cache_profile_append as fixture


class Phases(unittest.TestCase):
    def setUp(self):
        self.f=fixture.Profiles('test_exact_different_cpu_generation_appends_without_old_bytes_or_marker_change')
        self.f.setUp();self.addCleanup(self.f.doCleanups);self.stage=self.f.root/'protected-stage';self.stage.mkdir()
        self.request=copy.deepcopy(self.f.request);self.request['url']='https://fixture.blob.core.windows.net/verified'
        for item in (patch.object(j,'STAGE',self.stage),patch.object(j,'SHARED',self.f.shared)):
            item.start();self.addCleanup(item.stop)

    def receive(self,entry,received,candidate,api,**kwargs):
        self.assertEqual(entry,self.f.entry);shutil.copytree(self.f.candidate,candidate);return candidate/'index.json'

    def execute(self,phase):return j.execute(phase,self.f.entry,self.f.base,self.request)

    def test_stage_append_verify_only_new_objects_and_preserve_runtime_selection(self):
        with patch.object(j.transport,'receive',side_effect=self.receive):
            staged=self.execute('stage');self.assertFalse(staged['shared_cache_modified'])
        self.f.unchanged();self.assertFalse((self.f.shared/'indexes'/(self.f.ident+'.json')).exists())
        published=self.execute('append');self.assertEqual(published['compiler_invocations'],0)
        result=self.execute('verify');self.assertTrue(result['independently_verified'])
        self.assertFalse(result['compatibility_aliases']);self.f.unchanged()

    def test_stale_or_invalid_parent_provenance_fails_before_transfer(self):
        self.request['parent_authorizations'][0]['checked_at']='2000-01-01T00:00:00+00:00'
        with patch.object(j.transport,'receive') as transferred,self.assertRaises(ValueError):self.execute('stage')
        transferred.assert_not_called();self.f.unchanged()

    def test_missing_stage_or_wrong_phase_cannot_invoke_writer(self):
        for phase in ('append','verify','publish-initial'):
            with patch.object(j.core,'append') as writer,self.assertRaises((ValueError,FileNotFoundError)):self.execute(phase)
            writer.assert_not_called();self.f.unchanged()

    def test_verify_rechecks_actual_cpu_after_successful_append(self):
        with patch.object(j.transport,'receive',side_effect=self.receive):self.execute('stage')
        self.execute('append')
        with patch.object(j.core,'cpu_profile',return_value=fixture.CPU),self.assertRaisesRegex(ValueError,'actual target CPU'):
            self.execute('verify')
        self.f.unchanged()


if __name__=='__main__':unittest.main()
