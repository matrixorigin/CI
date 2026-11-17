import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile
import populate_reviewed_pvc as p


class PopulationTests(unittest.TestCase):
    def fixture(self, symlink=False):
        blob = b'compile-only-fixture'
        fp = {'source': 'a' * 40, 'cs_environment': 'jed', 'group': '1'}
        m = {'compile_only': True, 'fingerprint': fp, 'archive_bytes': len(blob),
             'archive_sha256': hashlib.sha256(blob).hexdigest()}
        manifest = json.dumps(m).encode()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            item = zipfile.ZipInfo('build-cache.tar.gz')
            if symlink:
                item.external_attr = 0o120777 << 16
            z.writestr(item, blob)
            z.writestr('manifest.json', manifest)
        payload = buffer.getvalue()
        entry = {'source_repository': 'matrixorigin/matrixone', 'source': fp['source'],
                 'environment': 'jed', 'group': '1', 'fingerprint': fp,
                 'manifest_sha256': hashlib.sha256(manifest).hexdigest(),
                 'artifact': {'id': 123, 'size_in_bytes': len(payload),
                              'digest': 'sha256:' + hashlib.sha256(payload).hexdigest()}}
        return {'url': 'https://example.invalid/fixture', 'entry': entry}, payload

    def run_fill(self, root, cfg, payload, free=30*p.GIB):
        with patch.dict(p.os.environ, {'CS_CACHE_ENV': 'jed', 'COVERAGE_GROUP': '1'}), \
             patch.object(p.urllib.request, 'urlopen', return_value=io.BytesIO(payload)), \
             patch.object(p.shutil, 'disk_usage', return_value=type('Space', (), {'free': free})()):
            return p.populate(cfg, root)

    def test_success_readonly_and_no_overwrite(self):
        cfg, payload = self.fixture()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = self.run_fill(root, cfg, payload)
            dest = root/'reviewed-123'
            self.assertTrue(result['verified'])
            self.assertEqual(result['environment'], 'jed')
            self.assertEqual(dest.stat().st_mode & 0o222, 0)
            with self.assertRaises(ValueError):
                self.run_fill(root, cfg, payload)
            # Only local unittest fixture; never remove live PVC cache data.
            dest.chmod(0o700)

    def test_environment_group_source_rejected_before_creation(self):
        cfg, payload = self.fixture()
        for field, value in [('environment', 'mty'), ('group', '0'), ('source', 'b'*40)]:
            bad = copy.deepcopy(cfg)
            bad['entry'][field] = value
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(ValueError):
                    self.run_fill(Path(d), bad, payload)
                self.assertEqual(list(Path(d).iterdir()), [])

    def test_corrupt_transport_or_manifest_rejected(self):
        for field in ('transport', 'manifest'):
            cfg, payload = self.fixture()
            if field == 'transport':
                payload = payload[:-1] + b'x'
            else:
                cfg['entry']['manifest_sha256'] = '0'*64
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(ValueError):
                    self.run_fill(Path(d), cfg, payload)
                self.assertFalse((Path(d)/'reviewed-123/population.json').exists())

    def test_symlink_member_rejected(self):
        cfg, payload = self.fixture(symlink=True)
        with tempfile.TemporaryDirectory() as d, self.assertRaises(ValueError):
            self.run_fill(Path(d), cfg, payload)

    def test_low_space_rejected_before_creation(self):
        cfg, payload = self.fixture()
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                self.run_fill(Path(d), cfg, payload, free=19*p.GIB)
            self.assertEqual(list(Path(d).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
