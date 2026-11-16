import tarfile
import unittest
import os
from pathlib import Path
import subprocess
import tempfile
import time
from unittest.mock import patch
from coverage_build_cache import (GIB, check_manifest, validate_entries,
                                 snapshot_native_inputs, restore_native_input_times,
                                 verify_native_hashes, sha256, coverage_contract,
                                 coverage_compile_command, verify_coverage_contract, run_compile)


class CoverageCompileContract(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.full = self.root / 'full'
        self.selected = self.root / 'selected'
        self.coverpkg = self.root / 'coverpkg'
        self.names = ['github.com/matrixorigin/matrixone/pkg/p' + str(i) for i in range(505)]
        self.full.write_text('\n'.join(self.names) + '\n')
        self.selected.write_text('\n'.join(self.names[:166]) + '\n')
        self.coverpkg.write_text(','.join(self.names[:484]) + '\n')

    def contract(self):
        return coverage_contract(self.selected, self.coverpkg, self.full)

    def test_full_scope_and_real_coverage_flags(self):
        contract = self.contract()
        command = coverage_compile_command(contract)
        self.assertIn('-c', command)
        self.assertEqual(command[command.index('-o') + 1], os.devnull)
        self.assertIn('-covermode=set', command)
        self.assertIn('-count=1', command)
        self.assertIn('-timeout=20m', command)
        self.assertEqual(command[-166:], self.names[:166])
        self.assertNotIn('-run', command)
        self.assertEqual(len(contract['coverpkg']), 484)

    def test_reject_bad_scope_and_changed_contract(self):
        original = self.contract()
        verify_coverage_contract({'coverage_contract': original}, self.contract())
        self.selected.write_text('\n'.join(reversed(self.names[:166])) + '\n')
        with self.assertRaises(ValueError):
            verify_coverage_contract({'coverage_contract': original}, self.contract())
        for bad in ('', self.names[0] + '\n' + self.names[0], '-run=TestOnly',
                    'github.com/matrixorigin/matrixone/../escape'):
            self.selected.write_text(bad)
            with self.assertRaises(ValueError):
                self.contract()

    def test_compile_refuses_low_space_before_start(self):
        with patch('coverage_build_cache.shutil.disk_usage') as space, \
                patch('coverage_build_cache.subprocess.Popen') as start:
            space.return_value.free = 19 * GIB
            with self.assertRaisesRegex(ValueError, 'headroom'):
                run_compile(['go', 'test', '-c'], self.root, {'RUNNER_TEMP': str(self.root)})
            start.assert_not_called()

    def test_running_compile_disk_guard_cleans_only_its_group(self):
        with patch('coverage_build_cache.shutil.disk_usage') as space, \
                patch('coverage_build_cache.subprocess.Popen') as start, \
                patch('coverage_build_cache.os.killpg') as kill:
            space.return_value.free = 60 * GIB
            child = start.return_value
            child.pid = 123456
            child.poll.side_effect = [None, 0]
            def first_wait(**kwargs):
                space.return_value.free = 19 * GIB
                raise subprocess.TimeoutExpired('compile', 10)
            calls = []
            def wait(**kwargs):
                calls.append(kwargs)
                return first_wait(**kwargs) if len(calls) == 1 else 0
            child.wait.side_effect = wait
            with self.assertRaisesRegex(ValueError, 'headroom'):
                run_compile(['go', 'test', '-c'], self.root, {'RUNNER_TEMP': str(self.root)})
            self.assertTrue(start.call_args.kwargs['start_new_session'])
            self.assertEqual([call.args[0] for call in kill.call_args_list], [123456, 123456])

    @unittest.skipUnless(os.environ.get('CACHE_GO_INTEGRATION') == '1', 'opt-in local Go fixture')
    def test_real_go_compile_only_then_every_test_reruns(self):
        # Two packages with the same basename also verify -o /dev/null works.
        module = 'example.invalid/cachefixture'
        (self.root / 'go.mod').write_text('module ' + module + '\n\ngo 1.23\n')
        names = []
        for parent in ('a', 'b'):
            package = self.root / parent / 'same'
            package.mkdir(parents=True)
            (package / 'value.go').write_text('package same\nfunc Value() int { return 7 }\n')
            marker = self.root / (parent + '.ran')
            (package / 'value_test.go').write_text(
                'package same\nimport ("os"; "testing")\n'
                'func TestMain(m *testing.M) { f,e:=os.OpenFile("' + str(marker) + '", '
                'os.O_CREATE|os.O_APPEND|os.O_WRONLY,0600); if e!=nil {panic(e)}; '
                'f.WriteString("run\\n"); f.Close(); os.Exit(m.Run()) }\n'
                'func TestValue(t *testing.T) { if Value()!=7 {t.Fatal("wrong value")} }\n')
            names.append(module + '/' + parent + '/same')
        contract = {'packages': names, 'coverpkg': names,
                    'flags': self.contract()['flags']}
        env = dict(os.environ, GOCACHE=str(self.root / 'cache'), GOWORK='off',
                   GOPROXY='off', GOTOOLCHAIN='local', GOFLAGS='')
        env.pop('GOROOT', None)
        subprocess.run(coverage_compile_command(contract), cwd=self.root, env=env,
                       check=True, capture_output=True, timeout=120)
        self.assertFalse((self.root / 'a.ran').exists())
        self.assertFalse((self.root / 'b.ran').exists())
        for count in (1, 2):
            result = subprocess.run(['go', 'test', '-x', *contract['flags'],
                                     '-coverpkg=' + ','.join(names),
                                     '-coverprofile=' + str(self.root / f'{count}.out'), *names],
                                    cwd=self.root, env=env, check=True, capture_output=True, timeout=120)
            self.assertNotIn(b'(cached)', result.stdout)
            for line in result.stderr.splitlines():
                if b'/compile ' in line:
                    self.assertNotIn(b'value.go', line)
                    self.assertNotIn(b'value_test.go', line)
            self.assertEqual((self.root / 'a.ran').read_text(), 'run\n' * count)
            self.assertEqual((self.root / 'b.ran').read_text(), 'run\n' * count)
        # Parallel package runs append duplicate dependency blocks in different
        # orders; compare the records rather than nondeterministic line order.
        self.assertEqual(sorted((self.root / '1.out').read_text().splitlines()),
                         sorted((self.root / '2.out').read_text().splitlines()))


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
