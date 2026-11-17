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
import node_cache_bundle as b
import node_cache_candidate as k
import publish_node_cache as p
from test_node_compile_cache import approval


class CandidateTests(unittest.TestCase):
    def test_failure_diagnostics_do_not_disclose_exception_text(self):
        try:
            raise ValueError('secret-token=https://private.example/?signature=hidden')
        except ValueError as exc:
            result = c.failure_report(exc)
        self.assertEqual(result['code'], 'unclassified')
        self.assertNotIn('secret', str(result))
        self.assertNotIn('private.example', str(result))
        self.assertEqual(c.failure_report(ValueError('cache disk reserve reached'))['code'], 'disk_reserve')

    def test_unique_failure_reports_preserve_all_causes(self):
        c.record_failure(self.root, ValueError('compile reserve reached'))
        c.record_failure(self.root, ValueError('new build disagrees with trusted action'))
        records = [c.read_json(p) for p in (self.root/'failure-reports').glob('*.json')]
        self.assertEqual(len(records), 2)
        self.assertEqual({r['code'] for r in records}, {'compile_reserve', 'trusted_action_conflict'})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.source = self.root/'source'; self.source.mkdir()
        self.runtime = {'environment': 'mty', 'repository': 'matrixorigin/matrixone'}
        self.shared = self.root/'shared'; self.local = self.root/'private'; self.stage = self.root/'bundle'
        self.shared.mkdir()
        self.cfg = {'schema': 2, 'runtime': self.runtime, 'local': str(self.local),
                    'shared': str(self.shared), 'indexes': [], 'reserve_bytes': 0}
        self.old = b'old compiled object'; self.old_id = hashlib.sha256(self.old).hexdigest()
        parent = {'schema': 2, 'runtime': self.runtime,
                  'actions': {'a'*64: {'output_id': self.old_id, 'size': len(self.old)}}}
        mid = hashlib.sha256(c.canonical(parent)).hexdigest()
        c.atomic_json(self.shared/'indexes'/(mid+'.json'), parent)
        c.atomic_file(self.shared/'objects'/self.old_id[:2]/self.old_id,
                      io.BytesIO(self.old), len(self.old), self.old_id, reserve=0)
        self.cfg['indexes'] = [mid]
        self.state = self.root/'state.json'
        c.atomic_json(self.state, {'schema': 2, 'config': self.cfg, 'source': str(self.source),
                                  'native_key': 'b'*64, 'native_inputs': {'Makefile': 'c'*64}})
        c.atomic_json(self.root/'native-result.json', {'native_hit': True, 'native_output_sha256': 'd'*64})
        self.contract = {'packages': ['github.com/matrixorigin/matrixone/pkg/a'],
                         'full_packages': ['github.com/matrixorigin/matrixone/pkg/a'],
                         'coverpkg': ['github.com/matrixorigin/matrixone/pkg/a'], 'flags': k.n.legacy.FLAGS}
        patches = [patch.object(shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100*c.GIB, 0, 100*c.GIB)),
                   patch.object(k.n, 'runtime', return_value=self.runtime),
                   patch.object(k.n, 'native_key', return_value=('b'*64, {'Makefile': 'c'*64})),
                   patch.object(k.n, 'native_digest', return_value='d'*64),
                   patch.object(k.n, 'command', return_value='a'*40),
                   patch.dict(os.environ, {'GOCACHEPROG': 'fixture', 'RUNNER_TEMP': str(self.root),
                                          'CGO_CFLAGS': '-DMO_NATIVE_CACHE_'+'d'*64+'=1',
                                          'CS_S3_SECRET': 'fixture-only', 'CS_CACHE_READ_TOKEN': 'fixture-only'})]
        for item in patches: item.start(); self.addCleanup(item.stop)

    def build(self, command, source, env):
        self.assertIn('-c', command); self.assertEqual(command[command.index('-o')+1], os.devnull)
        self.assertNotIn('CS_S3_SECRET', env); self.assertNotIn('CS_CACHE_READ_TOKEN', env)
        cache = c.Cache(self.cfg); cache.get('a'*64)
        data = b'only changed package'; digest = hashlib.sha256(data).hexdigest()
        cache.put('e'*64, digest, len(data), io.BytesIO(data))

    def frozen(self):
        m = k.freeze(self.state, self.contract, self.stage, execute=self.build)
        entry, _ = approval(m['index_sha256'], self.runtime)
        entry['native_manifests'] = m['native_manifests']
        entry['manifest_sha256'] = c.digest(self.stage/'manifest.json')
        (self.stage/'transport.zip').write_bytes(b'api-approved-zip')
        entry['artifact'].update(size_in_bytes=(self.stage/'transport.zip').stat().st_size,
                                 digest='sha256:'+c.digest(self.stage/'transport.zip'))
        def api(path):
            if '/runs/' in path:
                producer = entry['producer']
                return {**producer, 'repository': {'full_name': producer['repository']},
                        'status': 'completed', 'conclusion': 'success',
                        'actor': {'login': producer['actor']}, 'triggering_actor': {'login': producer['actor']}}
            return {**entry['artifact'], 'expired': False,
                    'workflow_run': {'id': 1, 'head_sha': 'a'*40}}
        return m, entry, api

    def test_frozen_delta_excludes_parent_and_tests_cannot_mutate_snapshot(self):
        m, entry, api = self.frozen()
        self.assertEqual(m['object_count'], 1)
        self.assertEqual(set(c.read_json(self.stage/'index.json')['actions']), {'e'*64})
        self.assertFalse((self.root/'acceptance.json').exists())
        before = c.digest(self.stage/'delta.tar.gz')
        data = b'after test'; c.Cache(self.cfg).put('f'*64, hashlib.sha256(data).hexdigest(), len(data), io.BytesIO(data))
        self.assertEqual(c.digest(self.stage/'delta.tar.gz'), before)
        target = self.root/'validated'; index = b.stage(self.stage, entry, target, api)
        receipt = p.publish(target, index, self.shared, entry, api)
        self.assertEqual(receipt['new_bytes'], len(b'only changed package'))
        self.assertEqual(c.digest(self.shared/'objects'/self.old_id[:2]/self.old_id), self.old_id)

    def test_candidate_approval_and_transport_are_not_self_authorized(self):
        _, entry, api = self.frozen()
        with self.assertRaises(ValueError): b.stage(self.stage, {**entry, 'manifest_sha256': '0'*64}, self.root/'no', api)
        (self.stage/'transport.zip').write_bytes(b'wrong')
        with self.assertRaises(ValueError): b.stage(self.stage, entry, self.root/'no', api)
        self.assertFalse((self.root/'no').exists())

    def test_failed_compile_or_corruption_cannot_complete_candidate(self):
        def failed(*args): raise ValueError('compile failed')
        with self.assertRaises(ValueError): k.freeze(self.state, self.contract, self.stage, execute=failed)
        self.assertFalse(self.stage.exists())
        self.local.mkdir(exist_ok=True); c.atomic_json(self.local/'validation-failed.json', {'failed': True})
        with self.assertRaises(ValueError): k.freeze(self.state, self.contract, self.stage, execute=lambda *args: None)
        self.assertFalse(self.stage.exists())

    def test_scope_changes_do_not_relax_membership_or_execute_test_binaries(self):
        paths = [self.root/name for name in ('selected', 'full', 'cover')]
        paths[0].write_text('github.com/matrixorigin/matrixone/pkg/a\n')
        paths[1].write_text('github.com/matrixorigin/matrixone/pkg/a\ngithub.com/matrixorigin/matrixone/pkg/b\n')
        paths[2].write_text('github.com/matrixorigin/matrixone/pkg/a,github.com/matrixorigin/matrixone/pkg/b')
        contract = k.scope(*paths)
        for command in k.compile_commands(contract):
            self.assertIn('-c', command); self.assertIn('-count=1', command); self.assertNotIn('-exec', command)
        paths[0].write_text('github.com/matrixorigin/matrixone/pkg/missing\n')
        with self.assertRaises(ValueError): k.scope(*paths)

    def test_archive_links_and_extra_members_fail_before_staging(self):
        m, entry, api = self.frozen()
        archive = self.stage/'delta.tar.gz'
        with tarfile.open(archive, 'r:gz') as tar:
            members = [(info, tar.extractfile(info).read()) for info in tar]
        with tarfile.open(archive, 'w:gz') as tar:
            for info, raw in members: tar.addfile(info, io.BytesIO(raw))
            info = tarfile.TarInfo('objects/link'); info.type = tarfile.SYMTYPE; info.linkname = '/outside'; tar.addfile(info)
        m.update(archive_sha256=c.digest(archive), archive_bytes=archive.stat().st_size)
        (self.stage/'manifest.json').chmod(0o600)
        (self.stage/'manifest.json').write_bytes(c.canonical(m))
        entry['manifest_sha256'] = c.digest(self.stage/'manifest.json')
        with self.assertRaises(ValueError): b.stage(self.stage, entry, self.root/'bad', api)
        self.assertFalse((self.root/'bad').exists())


if __name__ == '__main__': unittest.main()
