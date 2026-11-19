import copy
import unittest
import build_persistent_real_pr_workflow as persistent
import build_all_env_real_pr_workflow as original


class PersistentWorkflow(unittest.TestCase):
    def test_only_authority_materialization_and_additional_raw_receipts_change(self):
        expected=original.build();actual=persistent.build();normalized=copy.deepcopy(actual)
        normalized['name']=expected['name']
        job=normalized['jobs']['ut-coverage-linux-x86'];step=next(s for s in job['steps'] if s.get('id')=='node_cache')
        step['run']=step['run'].replace('node_cache_pvc_authority.py materialize','node_cache_pr_native_catalog.py materialize').replace(
            '--catalog "$RUNNER_TEMP/cs-reviewed-persistent-catalog.json"','--catalog "$RUNNER_TEMP/cs-reviewed-native-catalog.json"')
        report=next(s for s in job['steps'] if s.get('with',{}).get('name','').startswith('ut-coverage-report-'))
        suffix='${{ runner.temp }}/cs-reviewed-persistent-catalog.json\n${{ runner.temp }}/cs-node-cache/persistent-authorization.json\n'
        self.assertTrue(report['with']['path'].endswith(suffix));report['with']['path']=report['with']['path'][:-len(suffix)]
        self.assertEqual(normalized,expected)
        self.assertEqual((original.accepted.consumer.ROOT/persistent.TARGET).read_text(),persistent.render())
        self.assertEqual(job['strategy']['matrix']['group'],[0,1]);self.assertEqual(job['strategy']['max-parallel'],2)


if __name__=='__main__':unittest.main()
