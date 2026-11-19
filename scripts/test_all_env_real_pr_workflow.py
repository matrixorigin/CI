import copy
from pathlib import Path
import unittest
import build_all_env_real_pr_workflow as p


class ExplicitEnvironment(unittest.TestCase):
    def test_only_routes_and_explicit_dispatch_binding_differ_from_accepted_mty(self):
        before=p.accepted.build();after=p.build();expected=copy.deepcopy(before)
        expected['name']=after['name'];expected['on']['workflow_call']['inputs']=after['on']['workflow_call']['inputs']
        expected['jobs']['check_coverage_eligibility']['steps'].insert(0,
            after['jobs']['check_coverage_eligibility']['steps'][0])
        job=expected['jobs']['ut-coverage-linux-x86'];job['runs-on']=after['jobs']['ut-coverage-linux-x86']['runs-on']
        original_step=next(s for s in job['steps'] if s.get('id')=='node_cache')
        new_step=next(s for s in after['jobs']['ut-coverage-linux-x86']['steps'] if s.get('id')=='node_cache')
        original_step['env']['REQUESTED_READER_ENVIRONMENT']='${{ inputs.environment }}'
        original_step['run']=original_step['run'].replace('set -euo pipefail\n',
            'set -euo pipefail\ntest "$CS_CACHE_ENV" = "$REQUESTED_READER_ENVIRONMENT"\n',1)
        self.assertEqual(original_step,new_step);self.assertEqual(expected,after)
        self.assertEqual(job['strategy'],{'fail-fast':False,'max-parallel':2,'matrix':{'group':[0,1]}})
        self.assertIn("inputs.node_pair == '12'",job['runs-on'])
        self.assertIn('mty|jed|mnl2|ruh',after['jobs']['check_coverage_eligibility']['steps'][0]['run'])

    def test_generated_workflow_exact(self):
        self.assertEqual((p.accepted.consumer.ROOT/p.TARGET).read_text(),p.render())


if __name__=='__main__':unittest.main()
