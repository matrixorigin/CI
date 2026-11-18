import unittest
from pathlib import Path

import build_pr_node_cache_workflow as builder


class PRConsumerContract(unittest.TestCase):
    def setUp(self):
        self.w = builder.build()
        self.j = self.w['jobs']['ut-coverage-linux-x86']

    def test_fixed_two_groups_without_cross_pr_serialization(self):
        self.assertEqual(self.j['strategy'], {'fail-fast': False, 'max-parallel': 2, 'matrix': {'group': [0, 1]}})
        self.assertNotIn('concurrency', self.w)
        self.assertNotIn('concurrency', self.j)
        self.assertEqual(self.j['runs-on'], 'cs-node-cache-7c14g')

    def test_dynamic_pr_source_and_trust_gate(self):
        gate = self.w['jobs']['check_coverage_eligibility']
        self.assertIn("github.repository == 'matrixorigin/matrixone'", gate['if'])
        ids = {s.get('id') for s in gate['steps']}
        self.assertTrue({'check_in_org', 'check_safe_label', 'eligibility'} <= ids)
        source = next(s for s in self.j['steps'] if s.get('name') == 'Checkout MatrixOne source')
        self.assertEqual(source['with']['ref'], '${{ github.event.pull_request.head.sha }}')
        self.assertEqual(source['with']['repository'], '${{ github.event.pull_request.head.repo.full_name }}')
        self.assertFalse(source['with']['persist-credentials'])

    def test_no_experimental_source_allowlist_or_precompile(self):
        steps = self.j['steps']
        run = '\n'.join(s.get('run', '') for s in steps)
        self.assertNotIn('source_shas', run)
        self.assertNotIn('node_cache_validation.py', run)
        self.assertNotIn('node_cache_candidate.py', run)
        self.assertNotIn('compile-test', run)
        self.assertNotIn('precompile', run)
        self.assertFalse(any('download-artifact' in s.get('uses', '') for s in steps))
        self.assertTrue(any(s.get('id') == 'persistent_toolchain' for s in steps))
        self.assertNotIn('node-cache-delta', run)

    def test_local_tools_and_cache_integrity_still_required(self):
        steps = {s.get('id'): s for s in self.j['steps'] if s.get('id')}
        self.assertIn('node_cache_toolchain_store.py', steps['persistent_toolchain']['run'])
        self.assertIn('node_compile_cache.py accept', steps['node_cache_acceptance']['run'])
        self.assertIn('incremental_cache_validation_failure', steps['coverage_status']['run'])
        self.assertIn('test "${CS_CACHE_ENV:-}" = "$CS_S3_ENV"', steps['coverage_ut']['run'])
        self.assertEqual(steps['coverage_ut']['env']['COVERAGE_GROUP_COUNT'], '2')
        self.assertEqual(steps['coverage_ut']['env']['COVERAGE_UT_PACKAGE_PARALLELISM'], '7')
        self.assertEqual(steps['coverage_ut']['env']['COVERAGE_UT_TEST_TIMEOUT_SECONDS'], '3600')
        for s in self.j['steps']:
            if s.get('with', {}).get('repository') == '${{ job.workflow_repository }}':
                self.assertEqual(s['with']['ref'], '${{ job.workflow_sha }}')

    def test_generated_file_is_current_and_only_supported_inputs(self):
        self.assertEqual(set(self.w['on']['workflow_call']['inputs']), {'cache_seed', 'bvt_generation'})
        self.assertEqual((builder.ROOT / builder.TARGET).read_text(), builder.render())
        rendered = builder.render()
        for old in ('inputs.source_sha', 'inputs.group_count', 'inputs.node_cache_environment',
                    'steps.restore_reviewed_cache', 'inputs.node_cache_candidate'):
            self.assertNotIn(old, rendered)


if __name__ == '__main__':
    unittest.main()
