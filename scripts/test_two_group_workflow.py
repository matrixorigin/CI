import unittest
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]


class Workflow(unittest.TestCase):
    def test_two_jobs_and_unchanged_test_budget(self):
        text=(ROOT/'.github/workflows/coverage-buildcache-arm.yaml').read_text()
        workflow=yaml.safe_load(text)
        job=workflow['jobs']['ut-coverage-linux-x86']
        self.assertEqual(job['strategy'],{'fail-fast':False,'max-parallel':2,'matrix':{'include':[{'group':0},{'group':1}]}})
        self.assertEqual(job['runs-on'],"${{ format('cs-{0}-pvc-g{1}-cpe8k', inputs.target_environment, matrix.group) }}")
        for required in ['--group-count 2','COVERAGE_UT_PACKAGE_PARALLELISM: "7"','COVERAGE_UT_TEST_TIMEOUT_SECONDS: "3600"','verify-native','verify-scope','TestObjectStorages/cs-s3/minio/basic','62914560']:
            self.assertTrue(required in text,required)
        self.assertNotIn('selected/profiles/2/',text)
        self.assertLess(text.index('coverage_build_cache.py seed'),text.index('phase_start cs_s3_smoke'))

    def test_reference_and_only_cold_mty(self):
        text=(ROOT/'.github/workflows/coverage-mty-two-group.yaml').read_text()
        for value in ['test "$TARGET" = mty','test "$MODE" = cold','44032472330','6b0121525da561211b76e647c10f374304d79c25','scripts/verify_two_group_producer.py comparison']:
            self.assertIn(value,text)
        self.assertNotIn('workflow_dispatch:',text)


if __name__=='__main__':unittest.main()
