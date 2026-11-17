import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import coverage_build_cache as producer
import cs_coverage_cache as consumer


class TwoGroupConsumer(unittest.TestCase):
    def test_exact_producer_runtime_identity(self):
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == '/proc/cpuinfo':
                return 'vendor_id : fixture\nmodel name : fixture\nflags : fixture\n'
            return original_read(path, *args, **kwargs)
        def command(args, **kwargs):
            if args[:3] == ['git', 'rev-parse', 'HEAD']:
                return producer.SOURCE+'\n'
            if args[:2] == ['git', 'status']:
                return ''
            if args[:3] == ['go', 'env', '-json']:
                return json.dumps({'GOOS':'linux','GOARCH':'amd64','CGO_ENABLED':'1'})
            return 'fixture-toolchain\n'
        original_hash = consumer.sha256
        def digest(path):
            return 'a'*64 if str(path) == '/etc/os-release' else original_hash(path)
        env = {'CS_CACHE_ENV':'mty','COVERAGE_GROUP':'0',
               'RUNNER_NAME':'cs-mty-pvc-g0-cpe8k-replacement-pod',
               'GOMODCACHE':'/job/mod','GOCACHE':'/job/go',
               'GITHUB_RUN_ID':'1','GITHUB_RUN_ATTEMPT':'1'}
        with patch.dict(os.environ,env), patch.object(Path,'read_text',read), \
             patch.object(producer.subprocess,'check_output',side_effect=command), \
             patch.object(producer,'sha256',side_effect=digest), \
             patch.object(consumer,'sha256',side_effect=digest):
            current = consumer.fingerprint(Path('/job/source'))
            saved = producer.fingerprint(Path('/job/source'))
        self.assertEqual(consumer.compatibility(saved),current)
        self.assertEqual(current['group_count'],2)
        self.assertEqual(current['partition_policy'],hashlib.sha256(
            Path(producer.__file__).with_name('coverage_two_groups.py').read_bytes()).hexdigest())
        self.assertNotEqual(consumer.compatibility({k:v for k,v in saved.items()
            if k not in ('group_count','partition_policy')}),current)
        self.assertNotEqual(consumer.compatibility({**saved,'partition_policy':'wrong'}),current)

    def test_only_mty_two_group_pool_allowed(self):
        for env,group,runner in [('mty','0','cs-mty-pvc-g0-cpe8k-new'),
                                 ('mty','1','cs-mty-pvc-g1-cpe8k-new')]:
            with patch.dict(os.environ,CS_CACHE_ENV=env,COVERAGE_GROUP=group,RUNNER_NAME=runner):
                self.assertEqual(consumer.cache_environment(),'mty')
        for env,group,runner in [('mty','2','cs-mty-pvc-g2-cpe8k-new'),
                                 ('jed','0','cs-jed-pvc-g0-cpe8k-new'),
                                 ('mty','0','cs-mty-pvc-g1-cpe8k-new'),
                                 ('mty','0','unreviewed-pool')]:
            with patch.dict(os.environ,CS_CACHE_ENV=env,COVERAGE_GROUP=group,RUNNER_NAME=runner):
                with self.assertRaises(ValueError):consumer.cache_environment()

    def test_two_group_end_to_end_reviewed_contract(self):
        root=Path(__file__).parents[1]
        text=(root/'.github/workflows/coverage-mty-two-group-pvc-consumer.yaml').read_text()
        self.assertNotIn('DRAFT_CATALOG_NOT_REVIEWED',text)
        self.assertNotIn('0000000000000000000000000000000000000000',text)
        self.assertIn('test "$CACHE_SEED" = false',text)
        self.assertIn('test "$REUSE_CACHE" = true',text)
        entries=json.loads((root/'config/coverage-two-group-cache-catalog.json').read_text())['entries']
        self.assertEqual([e['artifact']['id'] for e in entries],[14322160882,14321007529])
        self.assertEqual([e['group'] for e in entries],['0','1'])
        self.assertTrue(all(e['environment']=='mty' and e['producer']['id']==44062673282 and e['fingerprint']['group_count']==2 for e in entries))
        self.assertIn('ref: 38fca550df9136dc13ae6a98223fb6f7f82b5780',text)
        self.assertNotIn('coverage_build_cache.py seed',text)
        self.assertNotIn('actions/cache',text)
        self.assertNotIn('- group: 2',text)
        self.assertNotIn('profiles/2/',text)
        self.assertIn('max-parallel: 2',text)
        self.assertEqual(text.count('--group-count 2'),3)
        self.assertIn('44062673282',text)
        self.assertNotIn('44036614881',text)
        self.assertIn('Require the existing reviewed cache (canary only)',text)
        self.assertIn('Select read-only persistent cache (no network artifact download)',text)
        for name in ('coverage_build_cache.py','coverage_two_groups.py'):
            self.assertIn(hashlib.sha256((root/'scripts'/name).read_bytes()).hexdigest(),text)


if __name__ == '__main__':unittest.main()
