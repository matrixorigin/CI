import subprocess
from pathlib import Path
import unittest
import yaml


class WorkflowTests(unittest.TestCase):
    def test_migration_is_isolated_read_only_and_never_rebuilds_or_tests(self):
        root=Path(__file__).resolve().parents[1]
        workflow=yaml.load((root/'.github/workflows/node-cache-migrate-isolated.yaml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['jobs']),{'authorize','migrate'})
        authorization=workflow['jobs']['authorize']['steps'][0]['run']
        self.assertIn('test "$GITHUB_RUN_ATTEMPT" = 1',authorization)
        self.assertIn('refs/heads/weilu-runner-test',authorization)
        for pool in ('cs-ruh-pvc-g0-rhprep','cs-mnl2-pvc-g0-vlleop'):self.assertIn(pool,authorization)
        job=workflow['jobs']['migrate']
        self.assertEqual(job['runs-on'],"${{ needs.authorize.outputs.pool }}")
        text=str(job)
        for forbidden in ('make cgo','make build','go test','publish_node_cache.py','kubectl','CS_S3_'):
            self.assertNotIn(forbidden,text)
        self.assertIn('node_cache_migration_job.py',text)
        self.assertEqual(workflow['permissions']['actions'],'read')

    def test_opt_in_only_no_existing_pool_or_job_matrix_change(self):
        root=Path(__file__).resolve().parents[1]
        path='.github/workflows/coverage-ut.yaml'
        current=yaml.load((root/path).read_text(),Loader=yaml.BaseLoader)
        original=yaml.load(subprocess.check_output(['git','show','0cb8cb45bfff56f5e44fb636fac9f17d09800480:'+path],cwd=root,text=True),Loader=yaml.BaseLoader)
        inputs=current['on']['workflow_call']['inputs']
        self.assertEqual(inputs['node_compile_cache']['default'],'false')
        for key,value in original['on']['workflow_call']['inputs'].items():self.assertEqual(inputs[key],value)
        self.assertEqual(set(current['jobs']),set(original['jobs']))
        job=current['jobs']['ut-coverage-linux-x86'];old=original['jobs']['ut-coverage-linux-x86']
        self.assertEqual(job['strategy'],old['strategy'])
        self.assertIn("|| 'cs-large-7c14g'",job['runs-on'])
        steps={s.get('id',s.get('name')):s for s in job['steps']}
        self.assertIn('!inputs.node_compile_cache',steps['reviewed_cache']['if'])
        self.assertNotIn('continue-on-error',steps['node_cache_acceptance'])
        self.assertIn('incremental_cache_validation_failure',steps['coverage_status']['run'])
        self.assertIn('-DMO_NATIVE_CACHE_${native_id}=1',steps['coverage_ut']['run'])
        self.assertIn('-count=1',steps['coverage_ut']['run'])
        self.assertNotIn('publish_node_cache.py publish',str(job))
        self.assertEqual(inputs['node_cache_candidate']['default'],'false')
        self.assertLess(steps['coverage_ut']['run'].index('node_cache_candidate.py'),
                        steps['coverage_ut']['run'].index('phase_start cs_s3_smoke'))
        self.assertIn("steps.node_cache_acceptance.outcome == 'success'",steps['upload_node_cache_candidate']['if'])
        self.assertNotIn('continue-on-error',steps['upload_node_cache_candidate'])
        for name in ('merge-coverage-groups','coverage-final-status'):
            self.assertEqual(current['jobs'][name],original['jobs'][name])


if __name__=='__main__':unittest.main()
