import os
import unittest
from unittest.mock import patch
import cs_coverage_cache as cache


class PvcEnvironmentBinding(unittest.TestCase):
    def test_exact_environment_group_and_pool_required(self):
        for env in ('mty', 'jed', 'ruh'):
            for group in ('0', '1', '2'):
                suffix = 'rhprep' if env == 'ruh' else 'cpe8k'
                settings = {'CS_CACHE_ENV': env, 'COVERAGE_GROUP': group,
                            'RUNNER_NAME': f'cs-{env}-pvc-g{group}-{suffix}-set-runner-pod'}
                with patch.dict(os.environ, settings):
                    self.assertEqual(cache.cache_environment(), env)
                for wrong in ('cs-mnl2-pvc-g0-vlleop-runner-pod',
                              f'cs-{env}-large-7c14g-runner-pod',
                              f'cs-{env}-pvc-g{(int(group)+1)%3}-{suffix}-runner-pod'):
                    with patch.dict(os.environ, {**settings, 'RUNNER_NAME': wrong}):
                        with self.assertRaises(ValueError):
                            cache.cache_environment()

    def test_missing_environment_rejected(self):
        with patch.dict(os.environ, {'CS_CACHE_ENV': ''}):
            with self.assertRaises(ValueError):
                cache.cache_environment()

    def test_environment_never_removed_from_compatibility(self):
        a = {'cs_environment': 'mty', 'run': '1', 'attempt': '1', 'policy': 'reviewed'}
        b = {**a, 'cs_environment': 'jed'}
        self.assertNotEqual(cache.compatibility(a), cache.compatibility(b))
        self.assertEqual(cache.compatibility(a), {'cs_environment': 'mty'})

if __name__ == '__main__':
    unittest.main()
