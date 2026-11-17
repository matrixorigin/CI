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
                       'ref: 8f2347197e916087660c57aacce7fdc3cb32c623'):
            self.assertIn(marker, t)
        for stale in ('43897279982', '43870157584', 'cs-mnl2-pvc-g'):
            self.assertNotIn(stale, t)
        self.assertNotIn('coverage_build_cache.py seed', t)

    def test_only_explicit_reviewed_mty_producer(self):
        import json
        c = json.loads((ROOT/'config/coverage-cache-catalog.json').read_text())
        self.assertEqual(c['schema'], 1)
        self.assertEqual({e['environment'] for e in c['entries']}, {'mty','jed','ruh'})
        ruh = [e for e in c['entries'] if e['environment']=='ruh']
        self.assertEqual(len(ruh), 3)
        self.assertEqual({e['artifact']['id'] for e in ruh}, {14311524223,14312128728,14312317022})
        self.assertEqual({e['producer']['id'] for e in ruh}, {44041833471})
        self.assertEqual({e['producer']['head_sha'] for e in ruh}, {'5555635094f3ebb36291f29a615968548b2d6223'})
        for e in ruh:
            self.assertEqual(e['source'], 'dbc7ca14ca8b01cfe6d9e9a151619ea4b5ba14ca')
            self.assertEqual(e['fingerprint']['cs_environment'], 'ruh')
            self.assertRegex(e['manifest_sha256'], r'^[0-9a-f]{64}$')
            self.assertRegex(e['artifact']['digest'], r'^sha256:[0-9a-f]{64}$')
        jed = [e for e in c['entries'] if e['environment']=='jed']
        self.assertEqual({e['artifact']['id'] for e in jed}, {14311103808,14312425656,14310499999})
        self.assertEqual({e['producer']['id'] for e in jed}, {44040765865})
        self.assertEqual({e['producer']['head_sha'] for e in jed}, {'5555635094f3ebb36291f29a615968548b2d6223'})
        c = dict(c, entries=[e for e in c['entries'] if e['environment']=='mty'])
        self.assertEqual(len(c['entries']), 3)
        self.assertEqual({e['group'] for e in c['entries']}, {'0','1','2'})
        self.assertEqual({e['artifact']['id'] for e in c['entries']}, {14310968185,14310263300,14310002919})
        for e in c['entries']:
            self.assertEqual(e['producer']['id'], 44036614881)
            self.assertEqual(e['producer']['head_sha'], 'd1521fc1b05d41b5c501a92ee202ab1659c2dd7d')
            self.assertEqual(e['source'], 'dbc7ca14ca8b01cfe6d9e9a151619ea4b5ba14ca')
            self.assertEqual(e['environment'], 'mty')
            self.assertEqual(e['fingerprint']['cs_environment'], 'mty')
            self.assertRegex(e['manifest_sha256'], r'^[0-9a-f]{64}$')
            self.assertRegex(e['artifact']['digest'], r'^sha256:[0-9a-f]{64}$')

if __name__ == '__main__':
    unittest.main()
