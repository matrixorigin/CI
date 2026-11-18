from pathlib import Path
import re
import subprocess
import unittest

import yaml

import node_cache_cpu_producer_job as producer


class Workflow(unittest.TestCase):
    def workflow(self):
        path = Path(__file__).resolve().parents[1]/'.github/workflows/node-cache-cpu-profile-producer.yaml'
        return path.read_text(), yaml.load(path.read_text(), Loader=yaml.BaseLoader)

    def test_trusted_dispatch_only_isolated_route_no_ut_s3_or_publication(self):
        text, value = self.workflow()
        self.assertEqual(set(value['on']), {'workflow_call'})
        self.assertEqual(set(value['on']['workflow_call']['inputs']), {'node'})
        self.assertEqual(value['permissions'], {'contents': 'read', 'actions': 'read'})
        authorize = value['jobs']['authorize']
        for guard in ('matrixorigin/matrixone', 'workflow_dispatch', 'refs/heads/weilu-runner-test', 'Ariznawlll', 'github.run_attempt == 1'):
            self.assertIn(guard, authorize['if'])
        job = value['jobs']['compile-only']
        self.assertEqual(job['runs-on'], '${{ needs.authorize.outputs.route }}')
        self.assertNotIn('strategy', job)
        for forbidden in ('setup-go@', 'setup-cmake@', 'download-artifact@', 'TEST_S3', 'CS_S3', 'TOKEN_ACTION', 'node_cache_profile_append', 'node_cache_publish'):
            self.assertNotIn(forbidden, text)
        uses = [s['uses'] for s in job['steps'] if 'uses' in s]
        self.assertTrue(all(re.fullmatch(r'actions/(checkout|upload-artifact)@[0-9a-f]{40}', u) for u in uses))
        checkout = next(s for s in job['steps'] if s.get('with', {}).get('repository') == 'matrixorigin/matrixone')
        self.assertEqual(checkout['with']['ref'], producer.SOURCE)
        helper = next(s for s in job['steps'] if s.get('with', {}).get('repository') == 'matrixorigin/CI')
        self.assertRegex(helper['with']['ref'], r'^[0-9a-f]{40}$')
        self.assertEqual(job['env']['GOMAXPROCS'], '7')
        self.assertEqual(job['env']['GOTOOLCHAIN'], 'local')

    def test_original_native_coverage_deadlines_full_scope_and_allowlisted_bundle(self):
        _, value = self.workflow()
        job = value['jobs']['compile-only']
        runs = '\n'.join(s.get('run', '') for s in job['steps'])
        self.assertIn('timeout --kill-after=30s 1800s', runs)
        self.assertIn('node_cache_cpu_producer_job.py freeze', runs)
        self.assertIn('--full "$RUNNER_TEMP/coverage-full-scope.txt"', runs)
        self.assertNotIn('go test ', runs)
        bootstrap = [s for s in job['steps'] if 'apt-get' in s.get('run', '')]
        self.assertEqual(len(bootstrap), 1)
        self.assertIn('build-essential cmake pkg-config python3 python-is-python3', bootstrap[0]['run'])
        self.assertIn('job_wide_zero_tool_downloads_claimed:false', bootstrap[0]['run'])
        self.assertIn('--destination "$RUNNER_TEMP/node-toolchain-install"', runs)
        token_steps = [s for s in job['steps'] if 'CS_CACHE_READ_TOKEN' in s.get('env', {})]
        self.assertEqual(len(token_steps), 1)
        self.assertNotIn('prepare-native', token_steps[0]['run'])
        uploads = [s for s in job['steps'] if s.get('uses', '').startswith('actions/upload-artifact@')]
        candidate = uploads[0]
        self.assertNotIn('if', candidate)  # Only prior-step success can upload a candidate.
        self.assertEqual([p.rsplit('/', 1)[-1] for p in candidate['with']['path'].splitlines()],
                         ['manifest.json', 'index.json', 'delta.tar.gz'])
        self.assertNotIn('cpu-profile-candidate', uploads[1]['with']['path'])

    def test_embedded_shells_parse(self):
        _, value = self.workflow()
        for job in value['jobs'].values():
            for step in job['steps']:
                if 'run' in step:
                    self.assertEqual(subprocess.run(['bash', '-n'], input=step['run'], text=True, capture_output=True).returncode, 0)


if __name__ == '__main__': unittest.main()
