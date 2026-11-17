import os
import unittest
from unittest.mock import patch
from coverage_build_cache import cache_environment, check_manifest, GIB

class EnvironmentBinding(unittest.TestCase):
    def test_allowed(self):
        for env in ('mty','jed'):
            with patch.dict(os.environ,{'CS_CACHE_ENV':env}):self.assertEqual(cache_environment(),env)
    def test_missing_or_other(self):
        for env in ('','mnl2','ruh','MTY'):
            with patch.dict(os.environ,{'CS_CACHE_ENV':env}):
                with self.assertRaises(ValueError):cache_environment()
    def test_cross_environment_refused(self):
        manifest={'fingerprint':{'cs_environment':'mty'},'unpacked_bytes':GIB}
        check_manifest(manifest,{'cs_environment':'mty'})
        with self.assertRaises(ValueError):check_manifest(manifest,{'cs_environment':'jed'})

if __name__=='__main__':unittest.main()
