import json
import unittest
import build_native_refresh_workflow as native
import build_pr_node_cache_workflow as consumer


class DedicatedNativeWorkflow(unittest.TestCase):
    def test_exact_generated_source_and_only_two_reviewed_nodes(self):
        self.assertEqual((consumer.ROOT/native.TARGET).read_text(),native.render())
        workflow=native.build();self.assertEqual(set(workflow['on']),{'workflow_call'})
        gate=workflow['jobs']['authorize'];self.assertIn("github.actor == 'Ariznawlll'",gate['if'])
        self.assertIn("github.run_attempt == 1",gate['if']);self.assertIn("refs/heads/weilu-runner-test",gate['if'])
        job=workflow['jobs']['native-only'];self.assertEqual(job['strategy']['matrix']['node'],['mo-standard-0','mo-standard-1'])
        self.assertEqual(job['strategy']['max-parallel'],2);self.assertFalse(job['strategy']['fail-fast'])
        request=json.loads((consumer.ROOT/'config/node-native-refresh-request.json').read_text())
        source=next(s for s in job['steps'] if s.get('name')=='Checkout exact canonical trusted native source')
        self.assertEqual(source['with']['repository'],'matrixorigin/matrixone')
        self.assertEqual(source['with']['ref'],request['source_sha']);self.assertFalse(source['with']['persist-credentials'])
        self.assertEqual(request['native_input_count'],196);self.assertTrue(request['native_inputs_equal_current_pr'])
        self.assertEqual(request['current_pr_head_sha'],'706e5fa741aa6905877dab3e42ae73768b118e2b')

    def test_no_ut_go_precompile_shared_writer_or_ordinary_consumer_change(self):
        text=native.render()
        for bad in ('go test','node_cache_candidate.py','node_cache_profile_append','TEST_S3FS','kubectl','publish_node_cache.py'):
            self.assertNotIn(bad,text)
        self.assertIn('node_cache_native_refresh.py preflight',text);self.assertIn('node_cache_native_refresh.py freeze',text)
        normal=consumer.render()
        self.assertNotIn('node_cache_native_refresh',normal)
        job=native.build()['jobs']['native-only'];build=next(s for s in job['steps'] if 'Build only' in s.get('name',''))
        self.assertEqual(build['timeout-minutes'],30)
        self.assertEqual(job['env']['COVERAGE_UT_CGO_TIMEOUT_SECONDS'],'1800')


if __name__=='__main__':unittest.main()
