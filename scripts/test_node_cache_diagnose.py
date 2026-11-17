import unittest
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]


class DiagnosisContract(unittest.TestCase):
    def setUp(self):
        self.doc=yaml.safe_load((ROOT/'.github/workflows/node-cache-diagnose.yaml').read_text())

    def test_only_failed_scopes_and_trusted_manual_entry(self):
        gate=self.doc['jobs']['check_coverage_eligibility']
        for value in ('Ariznawlll','workflow_dispatch','weilu-runner-test','matrixorigin/matrixone'):
            self.assertIn(value,gate['if'])
        self.assertIn('mty:1|mnl2:0',gate['steps'][-1]['run'])
        self.assertIn('test "$GROUP_COUNT" = 2',gate['steps'][-1]['run'])
        self.assertIn('test "$SEED" = false',gate['steps'][-1]['run'])

    def test_no_ut_s3_upload_or_shared_publication(self):
        job=self.doc['jobs']['diagnose']
        self.assertEqual(job['strategy']['max-parallel'],1)
        step=next(s for s in job['steps'] if s.get('id')=='coverage_ut')
        self.assertIn('node_cache_candidate.py',step['run'])
        self.assertIn('phase_start incremental_candidate',step['run'])
        for forbidden in ('phase_start cs_s3_smoke','run_coverage_ut.py','go test -mod=readonly', 'TEST_S3FS_CS'):
            self.assertNotIn(forbidden,step['run'])
        self.assertNotIn('TEST_S3FS_CS',step['env'])
        self.assertEqual(set(self.doc['jobs']),{'check_coverage_eligibility','diagnose'})
        uploads=[s for s in job['steps'] if 'upload-artifact' in s.get('uses','')]
        self.assertEqual(len(uploads),1)
        self.assertNotIn('delta.tar',uploads[0]['with']['path'])
        self.assertNotIn('config.json',uploads[0]['with']['path'])
        self.assertNotIn('.log',uploads[0]['with']['path'])
        self.assertIn('failure-reports/*.json',uploads[0]['with']['path'])

    def test_preserve_resource_and_cache_validation(self):
        job=self.doc['jobs']['diagnose']
        self.assertIn('node-cache-7c14g',job['runs-on'])
        cache=next(s for s in job['steps'] if s.get('id')=='node_cache')
        self.assertIn('(.entries | length) > 0',cache['run'])
        step=next(s for s in job['steps'] if s.get('id')=='coverage_ut')
        self.assertEqual(step['env']['GOMAXPROCS'],'7')
        self.assertEqual(step['env']['COVERAGE_UT_PACKAGE_PARALLELISM'],'7')
        for job in self.doc['jobs'].values():
            for step in job['steps']:
                if step.get('with',{}).get('repository')=='matrixorigin/CI':
                    self.assertRegex(step['with']['ref'],r'^[0-9a-f]{40}$')


if __name__=='__main__':unittest.main()
