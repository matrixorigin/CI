import copy
import hashlib
import io
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import incremental_cache as c
import migrate_node_cache as m
import node_cache_migration_job as job
from test_node_compile_cache import approval


class MigrationTests(unittest.TestCase):
    def test_diagnostics_redact_exception_messages_and_preserve_phase(self):
        def fail(progress):
            progress('artifact_api')
            raise PermissionError('PRIVATE-TOKEN https://private.invalid?secret=PRIVATE')
        with patch.object(job,'main',side_effect=fail),patch.dict(os.environ,{'RUNNER_TEMP':str(self.root)}):
            with patch('sys.stdout',new_callable=io.StringIO) as captured:self.assertEqual(job.execute(),1)
        report=c.read_json(self.root/'node-cache-migration-diagnostic.json')
        self.assertEqual(report['phase'],'artifact_api')
        self.assertEqual(report['error_type'],'PermissionError')
        self.assertNotIn('PRIVATE',captured.getvalue()+str(report))
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.artifact = self.root/'artifact'; self.artifact.mkdir()
        self.runtime = {k: 'fixture' for k in m.RUNTIME_FIELDS}
        self.runtime.update(schema=2, environment='mty', repository='matrixorigin/matrixone')
        self.fp = {k: self.runtime[k] for k in m.RUNTIME_FIELDS}
        self.fp.update(schema=1, source='a'*40, group='0', cs_environment='mty')
        self.action = 'a'*64; self.data = b'compiler output'; self.obj = hashlib.sha256(self.data).hexdigest()
        self.header = f'v1 {self.action} {self.obj} {len(self.data):20d} {1:20d}\n'.encode()
        self.members = [(f'gocache/aa/{self.action}-a', self.header),
                        (f'gocache/{self.obj[:2]}/{self.obj}-d', self.data),
                        ('native/cgo/libmo.so', b'old native'), ('gocache/trim.txt', b'0')]
        self.entry, _ = approval('0'*64, self.runtime)
        self.entry.update(source_repository='matrixorigin/matrixone', environment='mty',
                          source='a'*40, fingerprint=self.fp)
        self.pack()
        guard = patch.object(shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100*c.GIB, 0, 100*c.GIB))
        guard.start(); self.addCleanup(guard.stop)

    def api(self, path):
        p = self.entry['producer']
        if '/runs/' in path:
            return {**p, 'repository': {'full_name': p['repository']},
                    'status': 'completed', 'conclusion': 'success',
                    'actor': {'login': p['actor']}, 'triggering_actor': {'login': p['actor']}}
        return {**self.entry['artifact'], 'expired': False,
                'expires_at': '2099-01-01T00:00:00Z',
                'workflow_run': {'id': p['id'], 'head_sha': p['head_sha']}}

    def pack(self):
        archive = self.artifact/'build-cache.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name, data in self.members:
                info = tarfile.TarInfo(name); info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        manifest = {'compile_only': True, 'fingerprint': self.fp,
                    'native_hashes': {'cgo/libmo.so': hashlib.sha256(b'old native').hexdigest()},
                    'archive_bytes': archive.stat().st_size, 'archive_sha256': c.digest(archive),
                    'unpacked_bytes': sum(len(data) for _, data in self.members)}
        # Simulated API-approved transport. The migration verifies the transport
        # hash; extraction of the already present archive is independently pinned.
        (self.artifact/'transport.zip').write_bytes(b'reviewed transport')
        (self.artifact/'manifest.json').write_bytes(c.canonical(manifest))
        self.entry['manifest_sha256'] = c.digest(self.artifact/'manifest.json')
        self.entry['artifact'].update(size_in_bytes=18, digest='sha256:'+c.digest(self.artifact/'transport.zip'))

    def migrate(self, current=None, candidate=None, api=None):
        return m.migrate(self.artifact, self.entry, current or self.runtime,
                         candidate or self.root/'candidate', api or self.api)

    def test_reviewed_conversion_no_compiler_no_native_no_original_mutation(self):
        before = {p.name: c.digest(p) for p in self.artifact.iterdir()}
        with patch('subprocess.run', side_effect=AssertionError('must not compile')):
            receipt = self.migrate()
        self.assertEqual(receipt['action_count'], 1)
        self.assertEqual(receipt['compiler_invocations'], 0)
        self.assertFalse(receipt['native_imported'])
        self.assertFalse((self.root/'candidate/native').exists())
        self.assertEqual(before, {p.name: c.digest(p) for p in self.artifact.iterdir()})
        self.assertEqual(c.read_json(self.root/'candidate/index.json')['actions'][self.action]['output_id'], self.obj)
        with self.assertRaises(ValueError): self.migrate()

    def test_put_executable_covdata_layout_is_content_verified_and_flattened(self):
        self.members=[(name+'/covdata' if name.endswith('-d') else name,data) for name,data in self.members]
        self.pack()
        with patch('subprocess.run',side_effect=AssertionError('executable cache must never run')):
            result=self.migrate()
        self.assertEqual(result['action_count'],1)
        target=self.root/'candidate/objects'/self.obj[:2]/self.obj
        self.assertEqual(c.digest(target),self.obj)
        self.assertFalse(target.stat().st_mode & 0o111)
        self.assertEqual(result['compiler_invocations'],0)

    def test_testexpire_timestamp_validated_but_not_imported(self):
        self.assertEqual(m.disk_member('gocache/testexpire.txt'), ('', 'metadata'))
        self.members.append(('gocache/testexpire.txt',b'1794910000000000000\n'));self.pack()
        result=self.migrate();self.assertEqual(result['action_count'],1)
        self.assertFalse(any(p.name=='testexpire.txt' for p in (self.root/'candidate').rglob('*')))
        self.members[-1]=('gocache/testexpire.txt',b'not-a-timestamp');self.pack()
        with self.assertRaises(ValueError):self.migrate(candidate=self.root/'malformed')
        self.assertFalse((self.root/'malformed').exists())

    def test_executable_alias_corruption_and_nested_paths_rejected(self):
        original=list(self.members)
        for name,data in [(f'gocache/{self.obj[:2]}/{self.obj}-d/covdata',self.data),
                          (f'gocache/{self.obj[:2]}/{self.obj}-d/sub/covdata',self.data),
                          (f'gocache/{self.obj[:2]}/{self.obj}-d/../escape',self.data)]:
            self.members=original+[(name,data)];self.pack()
            with self.assertRaises(ValueError):self.migrate()
            self.assertFalse((self.root/'candidate').exists())
        self.members=[(name+'/covdata',b'corrupt') if name.endswith('-d') else (name,data) for name,data in original]
        self.pack()
        with self.assertRaises(ValueError):self.migrate()
        self.assertFalse((self.root/'candidate/index.json').exists())

    def test_corrupt_transport_manifest_archive_provenance_rejected_before_stage(self):
        for target in ('transport.zip', 'manifest.json', 'build-cache.tar.gz'):
            with self.subTest(target=target):
                path = self.artifact/target; original = path.read_bytes(); path.write_bytes(original+b'bad')
                with self.assertRaises(ValueError): self.migrate()
                self.assertFalse((self.root/'candidate').exists()); path.write_bytes(original)
        def failed(path):
            value = self.api(path)
            if '/runs/' in path: value['conclusion'] = 'failure'
            return value
        with self.assertRaises(ValueError): self.migrate(api=failed)
        self.assertFalse((self.root/'candidate').exists())

    def test_cross_environment_toolchain_path_rejected(self):
        for key in ('environment', 'repository', *m.RUNTIME_FIELDS):
            with self.subTest(key=key):
                with self.assertRaises(ValueError): self.migrate({**self.runtime, key: 'different'})
        self.assertFalse((self.root/'candidate').exists())

    def test_unknown_format_duplicate_traversal_and_native_corruption_fail(self):
        original = list(self.members)
        for bad in (('gocache/../escape', b'x'), original[0],
                    ('gocache/bb/'+'b'*64+'-a', b'unknown format'),
                    ('native/cgo/source.c', b'x')):
            self.members = original+[bad]; self.pack()
            with self.assertRaises(ValueError): self.migrate()
            self.assertFalse((self.root/'candidate').exists())
        self.members = [(name, b'corrupt' if name.startswith('native/') else data) for name, data in original]
        self.pack()
        with self.assertRaises(ValueError): self.migrate()

    def test_trimmed_go_object_is_omitted_not_recompiled(self):
        self.members = [v for v in self.members if not v[0].endswith('-d')]; self.pack()
        report = self.migrate()
        self.assertEqual((report['action_count'], report['trimmed_action_count']), (0, 1))

    def test_migration_bundle_preserves_source_expiry_and_never_compiles(self):
        receipt = self.migrate()
        with patch('subprocess.run', side_effect=AssertionError('must not compile')):
            manifest = job.package(self.root/'candidate', receipt, self.entry, self.root/'bundle')
        self.assertEqual(manifest['object_count'], 1)
        self.assertEqual(manifest['migration']['source_expires_at'], '2099-01-01T00:00:00+00:00')
        self.assertEqual(manifest['native_manifests'], {})
        with tarfile.open(self.root/'bundle/delta.tar.gz', 'r:gz') as tar:
            self.assertEqual(set(tar.getnames()), {'index.json', 'objects/'+self.obj[:2]+'/'+self.obj})
        with self.assertRaises(ValueError): job.package(self.root/'candidate', receipt, self.entry, self.root/'bundle')

    def test_corrupt_go_object_never_has_completed_index(self):
        self.members = [(name, b'bad compiler!!!' if name.endswith('-d') else data) for name, data in self.members]
        self.pack()
        with self.assertRaises(ValueError): self.migrate()
        self.assertFalse((self.root/'candidate/index.json').exists())

    def test_expiry_space_and_overlap_fail_closed(self):
        self.entry['expires_at'] = '2000-01-01T00:00:00Z'
        with self.assertRaises(ValueError): self.migrate()
        self.entry['expires_at'] = '2099-01-01T00:00:00Z'
        with patch.object(shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(1, 0, 1)):
            with self.assertRaises(ValueError): self.migrate()
        with self.assertRaises(ValueError): self.migrate(candidate=self.artifact/'nested')
        self.assertFalse((self.root/'candidate').exists())


if __name__ == '__main__': unittest.main()
