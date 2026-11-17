import copy
import json
from pathlib import Path
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[1]


class FullAcceptanceContract(unittest.TestCase):
    def setUp(self):
        self.base=yaml.safe_load((ROOT/'.github/workflows/coverage-ut.yaml').read_text())
        self.probe=yaml.safe_load((ROOT/'.github/workflows/node-cache-full-acceptance.yaml').read_text())

    def test_all_unrelated_ut_steps_identical(self):
        old=self.base['jobs']['ut-coverage-linux-x86']['steps'];new=self.probe['jobs']['ut-coverage-linux-x86']['steps']
        self.assertEqual(len(old),len(new))
        for original,changed in zip(old,new):
            restored=copy.deepcopy(changed)
            if original.get('name')=='Checkout MatrixOne source':
                restored['with']['repository']=original['with']['repository'];restored['with']['ref']=original['with']['ref']
            if original.get('name')=='Checkout CI coverage helper':
                self.assertEqual(restored['with']['repository'],'matrixorigin/CI')
                self.assertRegex(restored['with']['ref'],r'^[0-9a-f]{40}$')
                restored['with']['repository']=original['with']['repository'];restored['with']['ref']=original['with']['ref']
            if original.get('id')=='node_cache':
                self.assertIn('(.entries | length) > 0',restored['run'])
                restored['run']=original['run'];restored['env']['CS_CACHE_SOURCE_REPOSITORY']=original['env']['CS_CACHE_SOURCE_REPOSITORY']
            if original.get('id')=='coverage_ut':
                self.assertEqual(restored['env'].pop('COVERAGE_GROUP_COUNT'),'${{ inputs.group_count }}')
                restored['run']=restored['run'].replace(' --group-count "$COVERAGE_GROUP_COUNT"','')
            self.assertEqual(original,restored,original.get('name'))

    def test_fixed_source_and_trusted_actor_gate(self):
        job=self.probe['jobs']['check_coverage_eligibility']
        for marker in ('workflow_dispatch','weilu-runner-test','Ariznawlll','matrixorigin/matrixone'):
            self.assertIn(marker,job['if'])
        shell=job['steps'][-1]['run']
        for marker in ('node-cache-validation-sources.json','test "$NODE_CACHE" = true','test "$CANDIDATE" = true',
                       'test "$LEGACY" = false','test "$SEED" = false'):
            self.assertIn(marker,shell)
        policy=json.loads((ROOT/'config/node-cache-validation-sources.json').read_text())
        self.assertTrue(policy['source_shas'])
        for sha in policy['source_shas']:self.assertRegex(sha,r'^[0-9a-f]{40}$')

    def test_merge_all_declared_groups_and_expected_sha(self):
        strategy=self.probe['jobs']['ut-coverage-linux-x86']['strategy']
        self.assertFalse(strategy['fail-fast']);self.assertIn('[0,1,2]',strategy['matrix']['group'])
        step=next(s for s in self.probe['jobs']['merge-coverage-groups']['steps']
                  if s.get('name')=='Validate complete package evidence and merge profiles')
        self.assertEqual(step['env']['EXPECTED_HEAD_SHA'],'${{ inputs.source_sha }}')
        self.assertEqual(step['run'].count('--group-count "$GROUP_COUNT"'),2)
        self.assertIn('group<GROUP_COUNT',step['run'])
        self.assertEqual(self.probe['jobs']['coverage-final-status'],self.base['jobs']['coverage-final-status'])


if __name__=='__main__':unittest.main()
