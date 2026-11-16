import tarfile
import unittest
import os
from pathlib import Path
import subprocess
import tempfile
import time
from coverage_build_cache import (GIB, check_manifest, validate_entries,
                                 snapshot_native_inputs, restore_native_input_times,
                                 verify_native_hashes, sha256)


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


class FreshCheckoutRestore(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / 'cgo').mkdir()
        (self.root / 'thirdparties').mkdir()
        (self.root / 'Makefile').write_text(
            'all: cgo/native.o\ncgo/native.o: cgo/native.c\n\t@echo rebuilt > cgo/native.o\n')
        (self.root / 'cgo/native.c').write_text('int fixture;\n')
        (self.root / 'thirdparties/config').write_text('fixed\n')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', 'add', 'Makefile', 'cgo', 'thirdparties'], cwd=self.root, check=True)
        self.base = time.time_ns() - 60_000_000_000
        for name in ('Makefile', 'cgo/native.c', 'thirdparties/config'):
            os.utime(self.root / name, ns=(self.base, self.base))
        self.recorded = snapshot_native_inputs(self.root)
        self.output = self.root / 'cgo/native.o'
        self.output.write_bytes(b'original object')
        os.utime(self.output, ns=(self.base + 10_000_000_000,) * 2)
        self.hashes = {'cgo/native.o': sha256(self.output)}
        # Model a new checkout on another runner, keeping cached outputs old.
        for name in self.recorded:
            os.utime(self.root / name, ns=(self.base + 20_000_000_000,) * 2)

    def test_new_checkout_reproduces_rebuild_then_restores_noop(self):
        def make_query():
            return subprocess.run(['make', '-q'], cwd=self.root).returncode
        self.assertEqual(make_query(), 1)
        output_time = self.output.stat().st_mtime_ns
        restore_native_input_times(self.root, self.recorded)
        self.assertEqual(make_query(), 0)
        subprocess.run(['make'], cwd=self.root, check=True, capture_output=True)
        verify_native_hashes(self.root, self.hashes)
        self.assertEqual(self.output.stat().st_mtime_ns, output_time)

    def test_changed_input_rejected_before_any_timestamp_write(self):
        (self.root / 'thirdparties/config').write_text('changed\n')
        before = {n: (self.root / n).stat().st_mtime_ns for n in self.recorded}
        with self.assertRaisesRegex(ValueError, 'content mismatch'):
            restore_native_input_times(self.root, self.recorded)
        self.assertEqual(before, {n: (self.root / n).stat().st_mtime_ns for n in self.recorded})

    def test_missing_or_extra_input_rejected(self):
        for names in (dict(list(self.recorded.items())[1:]), dict(self.recorded, **{'../extra': {}})):
            with self.assertRaisesRegex(ValueError, 'scope mismatch'):
                restore_native_input_times(self.root, names)

    def test_changed_native_output_rejected(self):
        self.output.write_bytes(b'different object')
        with self.assertRaisesRegex(ValueError, 'native bytes changed'):
            verify_native_hashes(self.root, self.hashes)

    def test_future_timestamp_rejected(self):
        self.recorded['Makefile']['mtime_ns'] = time.time_ns() + 60_000_000_000
        with self.assertRaisesRegex(ValueError, 'invalid native input time'):
            restore_native_input_times(self.root, self.recorded)

    def test_symlink_input_rejected(self):
        target = self.root / 'cgo/native.c'
        target.unlink()
        target.symlink_to(self.root / 'thirdparties/config')
        with self.assertRaisesRegex(ValueError, 'content mismatch'):
            restore_native_input_times(self.root, self.recorded)

    @unittest.skipUnless(os.environ.get('MO_NATIVE_PROVENANCE'), 'requires actual MO provenance script')
    def test_actual_mo_provenance_accepts_noop_and_rejects_mutation(self):
        script = self.root / 'cgo/mo-native-provenance'
        script.write_bytes(Path(os.environ['MO_NATIVE_PROVENANCE']).read_bytes())
        script.chmod(0o755)
        (self.root / '.gitignore').write_text(
            'cgo/*.o\ncgo/*.so\ncgo/.mo-native-provenance*\nthirdparties/install/\n')
        subprocess.run(['git', 'add', '-f', '.gitignore', 'cgo/mo-native-provenance'], cwd=self.root, check=True)
        subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                        'commit', '-qm', 'fixture'], cwd=self.root, check=True)
        fake_go = self.root / 'go-fixture'
        fake_go.write_text('#!/bin/sh\ncase "$2" in GOHOSTOS) echo linux;; GOHOSTARCH) echo amd64;; *) exit 1;; esac\n')
        fake_go.chmod(0o755)
        env = dict(os.environ, GO=str(fake_go))
        def native(mode, check=True):
            return subprocess.run([str(script), mode, str(self.root), 'cpu', 'release', '0'],
                                  env=env, capture_output=True, text=True, check=check)
        self.output.unlink()  # Only this fixture's output; begin requires a clean generation.
        self.assertEqual(native('prepare').stdout.strip(), 'rebuild-all')
        native('begin')
        (self.root / 'thirdparties/install/include').mkdir(parents=True)
        (self.root / 'thirdparties/install/lib').mkdir()
        (self.root / 'thirdparties/install/lib/fixture.a').write_bytes(b'library fixture')
        library = self.root / 'cgo/libmo.so'
        library.write_bytes(b'native library fixture')
        self.output.write_bytes(b'object fixture')
        native('record')
        recorded = snapshot_native_inputs(self.root)
        output_hash = sha256(library)
        for name in recorded:
            # Separate seconds for older make implementations with coarse mtimes.
            os.utime(self.root / name, ns=(time.time_ns() + 5_000_000_000,) * 2)
        self.assertEqual(subprocess.run(['make', '-q'], cwd=self.root).returncode, 1)
        restore_native_input_times(self.root, recorded)
        self.assertEqual(native('prepare').stdout.strip(), 'reuse')
        native('begin')
        self.assertEqual(subprocess.run(['make', '-q'], cwd=self.root).returncode, 0)
        native('record')
        self.assertEqual(sha256(library), output_hash)
        self.assertEqual(native('prepare').stdout.strip(), 'reuse')
        native('begin')
        library.write_bytes(b'changed library fixture')
        self.assertNotEqual(native('record', check=False).returncode, 0)


if __name__ == '__main__':
    unittest.main()
