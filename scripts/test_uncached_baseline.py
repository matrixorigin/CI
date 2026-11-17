"""Prove the baseline control path does not prepare, restore or export a seed."""
import pathlib
import re
import subprocess
import json
import os
import tempfile
import textwrap
import unittest

ARM = pathlib.Path(__file__).resolve().parents[1] / '.github/workflows/coverage-buildcache-arm.yaml'


class BaselineContract(unittest.TestCase):
    def setUp(self):
        self.text = ARM.read_text()

    def test_native_cold_baseline_warm_paths(self):
        block = self.text.split('          phase_start prepare_cgo\n', 1)[1].split('          phase_status=$?', 1)[0]
        # Replace only command bodies; execute the real conditional in bash.
        block = re.sub(r'timeout --kill-after=30s[\s\S]*?> "\$\{cgo_log\}" 2>&1',
                       lambda m: "echo fresh" if 'prepare_coverage_cgo.sh' in m[0] else "echo verified_restore", block)
        for mode, expected in [('baseline', 'fresh'), ('cold', 'fresh'), ('warm', 'verified_restore')]:
            out = subprocess.check_output(['bash', '-c', 'CACHE_MODE=' + mode + '\n' + block], text=True)
            self.assertEqual(out.strip(), expected)

    def test_seed_only_cold(self):
        block = self.text.split('          # Snapshot only build artifacts', 1)[1].split('          # Fail early', 1)[0]
        self.assertIn('if [ "$CACHE_MODE" = cold ]; then', block)
        self.assertNotIn('baseline', block)
        self.assertEqual(self.text.count('coverage_build_cache.py seed '), 1)
        for name in ['Download this run group compile-only artifact', 'Validate and restore native and Go build caches']:
            self.assertIn('- name: ' + name + "\n        if: ${{ inputs.cache_mode == 'warm' }}", self.text)
        self.assertIn("- name: Upload frozen compile-only seed\n        if: ${{ inputs.cache_mode == 'cold'", self.text)

    def test_same_safety_and_test_contract(self):
        for invariant in ['max-parallel: 3', 'fail-fast: false', "format('cs-ruh-pvc-g{0}-rhprep'", 'test "$TARGET_ENV" = ruh',
                          'test "$CS_S3_ENV" = "$CS_CACHE_ENV"',
                          'cache: false', 'test ! -e "$RUNNER_TEMP/cs-buildcache-gomod"',
                          'test ! -e "$RUNNER_TEMP/cs-buildcache-go"', '62914560',
                          'check_buildcache_versions.py --source-dir', '-count=1',
                          'COVERAGE_UT_TEST_TIMEOUT_SECONDS: "3600"', 'verify-native', 'verify-scope',
                          'baseline-environment.json', 'ut-coverage-phase-timing.txt']:
            self.assertIn(invariant, self.text)
        self.assertNotIn('uses: actions/cache', self.text)

    def test_comparison_accepts_matching_evidence_rejects_changed_source_or_environment(self):
        wrapper = ARM.with_name('coverage-uncached-baseline.yaml').read_text()
        code = wrapper.split('- name: Compare source environment all packages skips and instrumentation', 1)[1]
        code = textwrap.dedent(code.split("python3 - <<'PY'\n", 1)[1].split('\n          PY', 1)[0])
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / 'comparison'
            manifest = {'source_sha': 'dbc7ca14ca8b01cfe6d9e9a151619ea4b5ba14ca', 'storage_mode': 'cs-s3',
                        'failed': [], 'package_timeouts': [], 'malformed_events': [],
                        'started_packages': [str(i) for i in range(505)], 'skipped_tests': []}
            for arm in ('baseline', 'warm'):
                (root/arm/'skips').mkdir(parents=True)
                (root/arm/'profile').mkdir()
                (root/arm/'skips/combined-skip-manifest.json').write_text(json.dumps(manifest))
                (root/arm/'profile/ut-coverage.out').write_text('mode: set\na.go:1.1,2.1 1 1\n')
            for group in range(3):
                current = root/'baseline/reports'/f'baseline-ut-coverage-report-group-{group}-attempt-1/baseline-environment.json'
                current.parent.mkdir(parents=True)
                reference = root/'warm/reports'/f'warm-ut-coverage-report-group-{group}-attempt-1/build-cache-artifact/manifest.json'
                reference.parent.mkdir(parents=True)
                fp = {'source': manifest['source_sha'], 'group': str(group), 'tools': {'go': 'same'}, 'run': '1', 'attempt': '1'}
                reference.write_text(json.dumps({'fingerprint': fp}))
                fp['run'] = '2'
                current.write_text(json.dumps({'mode': 'baseline', 'restored_cache': False, 'seed_precompile': False, 'fingerprint': fp}))
            def check():
                return subprocess.run(['python3', '-c', code], cwd=tmp, env={**os.environ, 'GITHUB_RUN_ID': '2'}, capture_output=True)
            self.assertEqual(check().returncode, 0, check().stderr.decode())
            changed = json.loads(current.read_text())
            changed['fingerprint']['tools']['go'] = 'different'
            current.write_text(json.dumps(changed))
            self.assertNotEqual(check().returncode, 0)
            changed['fingerprint']['tools']['go'] = 'same'
            current.write_text(json.dumps(changed))
            manifest['source_sha'] = '0' * 40
            (root/'baseline/skips/combined-skip-manifest.json').write_text(json.dumps(manifest))
            self.assertNotEqual(check().returncode, 0)


if __name__ == '__main__':
    unittest.main()
