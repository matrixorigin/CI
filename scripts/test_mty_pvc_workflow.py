from pathlib import Path
import unittest

ROOT = Path(__file__).parents[1]

class MtyWorkflow(unittest.TestCase):
    def test_exact_source_baseline_producer_and_pool(self):
        t = (ROOT/'.github/workflows/coverage-mty-pvc-consumer.yaml').read_text()
        for marker in ('cs-mty-pvc-g{0}-cpe8k', 'CS_CACHE_ENV: mty',
                       'test "$CS_S3_ENV" = "$CS_CACHE_ENV"',
                       '44032472330', '44036614881',
                       "entry['fingerprint']['cs_environment']=='mty'",
                       "clean(baseline['fingerprint'])==clean(entry['fingerprint'])",
                       "manifest['coverage_contract']['coverpkg']))==484",
                       "manifest['coverage_contract']['full_packages']))==505",
                       "test \"$HIT\" = true", "'PVC must be read-only'",
                       'ref: f6f2424c2e76a41b15b09ef007444d8def9c65b1'):
            self.assertIn(marker, t)
        for stale in ('43897279982', '43870157584', 'cs-mnl2-pvc-g'):
            self.assertNotIn(stale, t)
        self.assertNotIn('coverage_build_cache.py seed', t)

    def test_no_reviewed_artifacts_before_producer_acceptance(self):
        import json
        c = json.loads((ROOT/'config/coverage-cache-catalog.json').read_text())
        self.assertEqual(c, {'schema': 1, 'entries': []})

if __name__ == '__main__':
    unittest.main()
