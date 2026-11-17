import datetime as dt
import os
import unittest
from unittest.mock import patch
import incremental_cache as c
import node_cache_bundle as b
import node_cache_publish_job as j
import test_node_cache_candidate as fixtures

class PublisherJobTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.CandidateTests('test_frozen_delta_excludes_parent_and_tests_cannot_mutate_snapshot')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.m,self.entry,self.api=self.f.frozen()
        self.stage=self.f.root/'publish-stage';self.stage.mkdir()
        self.shared=self.f.root/'new-node-tree'
        b.stage(self.f.stage,self.entry,self.stage/'candidate',self.api)
        c.atomic_json(self.stage/'stage-receipt.json',{'index_sha256':self.entry['index_sha256']})
        p=self.entry['producer']
        self.request={'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),
           'run':self.api(f'repos/{p["repository"]}/actions/runs/{p["id"]}'),
           'artifact':self.api(f'repos/{p["repository"]}/actions/artifacts/{self.entry["artifact"]["id"]}'),
           'binding':{'environment':'mty','node':'mo-standard-0','node_uid':'11111111-1111-1111-1111-111111111111',
                      'pvc_uid':'22222222-2222-2222-2222-222222222222','pv_uid':'33333333-3333-3333-3333-333333333333'}}
        for p in [patch.object(j,'STAGE',self.stage),patch.object(j,'SHARED',self.shared),
                  patch.dict(os.environ,{'CS_CACHE_ENV':'mty','ACTUAL_NODE':'mo-standard-0'})]:
            p.start();self.addCleanup(p.stop)
    def test_initial_publish_and_independent_verify(self):
        report=j.execute('publish',self.entry,self.request)
        self.assertEqual(report['compiler_invocations'],0)
        self.assertEqual(j.execute('verify',self.entry,self.request)['object_count'],1)
        with self.assertRaises(ValueError):j.execute('publish',self.entry,self.request)
    def test_stale_metadata_does_not_write(self):
        self.request['checked_at']='2000-01-01T00:00:00+00:00'
        with self.assertRaises(ValueError):j.execute('publish',self.entry,self.request)
        self.assertFalse(self.shared.exists())
    def test_wrong_environment_and_node_refused(self):
        for key,value in [('environment','jed'),('node','mo-standard-1'),('pv_uid','bad')]:
            request={**self.request,'binding':{**self.request['binding'],key:value}}
            with self.assertRaises(ValueError):j.execute('publish',self.entry,request)
        self.assertFalse(self.shared.exists())
    def test_unknown_producer_refused(self):
        self.request['run']['head_sha']='f'*40
        with self.assertRaises(ValueError):j.execute('publish',self.entry,self.request)
        self.assertFalse(self.shared.exists())
    def test_corrupt_stage_cannot_publish(self):
        path=next((self.stage/'candidate/objects').glob('*/*'));path.chmod(0o600);path.write_bytes(b'bad')
        with self.assertRaises(ValueError):j.execute('publish',self.entry,self.request)
        self.assertFalse(self.shared.exists())
    def test_corrupt_published_data_cannot_verify(self):
        j.execute('publish',self.entry,self.request)
        path=next((self.shared/'objects').glob('*/*'));path.chmod(0o600);path.write_bytes(b'bad')
        with self.assertRaises(ValueError):j.execute('verify',self.entry,self.request)
    def test_url_origin_validation(self):
        for url in ('http://a.blob.core.windows.net/x','https://example.org/x','https://a.blob.core.windows.net.evil.test/x','https://user@a.blob.core.windows.net/x'):
            with self.assertRaises(ValueError):j.validate_url(url)
        self.assertEqual(j.validate_url('https://a.blob.core.windows.net/x'),'https://a.blob.core.windows.net/x')
if __name__=='__main__':unittest.main()
