"""Prove the baseline control path does not prepare, restore or export a seed."""
import pathlib
import re
import subprocess
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
        for invariant in ['max-parallel: 3', 'fail-fast: false', 'runs-on: cs-mnl2-large-7c14g',
                          'cache: false', 'test ! -e "$RUNNER_TEMP/cs-buildcache-gomod"',
                          'test ! -e "$RUNNER_TEMP/cs-buildcache-go"', '62914560',
                          'check_buildcache_versions.py --source-dir', '-count=1',
                          'COVERAGE_UT_TEST_TIMEOUT_SECONDS: "3600"', 'verify-native', 'verify-scope',
                          'baseline-environment.json', 'ut-coverage-phase-timing.txt']:
            self.assertIn(invariant, self.text)
        self.assertNotIn('uses: actions/cache', self.text)


if __name__ == '__main__':
    unittest.main()
