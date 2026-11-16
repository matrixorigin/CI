import tarfile
import unittest
from coverage_build_cache import GIB, check_manifest, validate_entries


class CacheContract(unittest.TestCase):
    def test_exact_provenance(self):
        expected = {'source': 'fixed', 'group': '0', 'run': '123', 'tools': 'same'}
        manifest = {'fingerprint': expected, 'unpacked_bytes': 100}
        check_manifest(manifest, expected)
        for key in expected:
            changed = dict(expected, **{key: 'different'})
            with self.assertRaises(ValueError):
                check_manifest(manifest, changed)

    def test_size_limit(self):
        for size in (-1, 0, 9*GIB):
            with self.assertRaises(ValueError):
                check_manifest({'fingerprint': {}, 'unpacked_bytes': size}, {})

    def test_size_mismatch(self):
        item = tarfile.TarInfo('native/cgo/libmo.so')
        item.size = 10
        validate_entries([item], 10)
        with self.assertRaises(ValueError):
            validate_entries([item], 11)

    def test_links_and_escape(self):
        for name in ('/etc/config', 'native/../../config', 'native/go.mod'):
            with self.assertRaises(ValueError):
                validate_entries([tarfile.TarInfo(name)], 0)
        item = tarfile.TarInfo('gocache/ab/cache-d')
        item.type = tarfile.SYMTYPE
        with self.assertRaises(ValueError):
            validate_entries([item], 0)


if __name__ == '__main__':
    unittest.main()
