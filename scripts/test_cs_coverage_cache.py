import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

import cs_coverage_cache as c


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root/'source'
        self.source.mkdir()
        (self.source/'cgo').mkdir()
        (self.source/'cgo/input.c').write_text('fixture')
        subprocess.run(['git', 'init', '-q', str(self.source)], check=True)
        subprocess.run(['git', '-C', str(self.source), 'add', 'cgo/input.c'], check=True)
        self.artifact = self.root/'artifact'
        self.artifact.mkdir()
        self.cache = self.root/'cache'
        self.manifest = {'compile_only': True, 'native_inputs': {'cgo/input.c': {
            'sha256': c.sha256(self.source/'cgo/input.c'), 'mtime_ns': time.time_ns()-1_000_000_000}},
            'native_hashes': {'cgo/libmo.so': c.hashlib.sha256(b'native').hexdigest()},
            'coverage_contract': {'packages': ['p/a'], 'full_packages': ['p/a', 'p/b'], 'coverpkg': ['p/a'], 'flags': c.FLAGS}}
        self.pack([('native/cgo/libmo.so', b'native'), ('gocache/aa/item', b'compiler')])

    def pack(self, files, extra=None):
        path = self.artifact/'build-cache.tar.gz'
        size = 0
        with tarfile.open(path, 'w:gz') as tar:
            for name, data in files:
                m = tarfile.TarInfo(name)
                m.size, m.mtime, m.mode = len(data), 123456, 0o644
                tar.addfile(m, io.BytesIO(data))
                size += len(data)
            if extra:
                tar.addfile(extra)
        self.manifest.update(archive_bytes=path.stat().st_size, archive_sha256=c.sha256(path), unpacked_bytes=size)

    def restore(self):
        with patch.object(c.shutil, 'disk_usage', return_value=type('Space', (), {'free': 80*c.GIB})()):
            c.restore(self.source, self.cache, self.artifact, self.manifest)

    def test_restore_contents_ordering_and_native_verification(self):
        self.restore()
        self.assertEqual((self.cache/'aa/item').read_bytes(), b'compiler')
        self.assertEqual((self.source/'cgo/input.c').stat().st_mtime_ns, self.manifest['native_inputs']['cgo/input.c']['mtime_ns'])
        c.verify_native(self.source, self.manifest)
        (self.source/'cgo/libmo.so').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'native bytes changed'):
            c.verify_native(self.source, self.manifest, stamp_refresh=True)

    def test_restored_input_order_prevents_unnecessary_make_rebuild(self):
        (self.source/'Makefile').write_text('all: cgo/libmo.so\ncgo/libmo.so: cgo/input.c\n\t@echo rebuild > cgo/libmo.so\n')
        subprocess.run(['git','-C',str(self.source),'add','Makefile'],check=True)
        self.manifest['native_inputs']['Makefile'] = {'sha256':c.sha256(self.source/'Makefile'),'mtime_ns':100_000_000_000}
        self.manifest['native_inputs']['cgo/input.c']['mtime_ns'] = 100_000_000_000
        self.restore()
        self.assertEqual(subprocess.run(['make','-q'],cwd=self.source).returncode,0)
        subprocess.run(['make'],cwd=self.source,check=True,capture_output=True)
        c.verify_native(self.source,self.manifest)

    def test_corrupt_archive_rejected_before_writes(self):
        with (self.artifact/'build-cache.tar.gz').open('ab') as f:
            f.write(b'bad')
        with self.assertRaises(ValueError):
            self.restore()
        self.assertFalse(self.cache.exists())
        self.assertFalse((self.source/'cgo/libmo.so').exists())

    def test_inputs_content_or_set_must_match(self):
        for mode in ('changed', 'missing'):
            with self.subTest(mode=mode):
                saved = copy.deepcopy(self.manifest['native_inputs'])
                if mode == 'changed':
                    self.manifest['native_inputs']['cgo/input.c']['sha256'] = '0'*64
                else:
                    self.manifest['native_inputs'] = {}
                with self.assertRaises(ValueError):
                    self.restore()
                self.assertFalse(self.cache.exists())
                self.manifest['native_inputs'] = saved

    def test_reject_nonregular_and_outside_archive_paths(self):
        for name in ('native/../other', '/outside', 'native/cgo/source.c', 'gocache//item'):
            with self.subTest(name=name):
                self.pack([(name, b'data')])
                with self.assertRaises(ValueError):
                    self.restore()
                self.assertFalse(self.cache.exists())
        link = tarfile.TarInfo('gocache/link')
        link.type, link.linkname = tarfile.SYMTYPE, 'outside'
        self.pack([('native/cgo/libmo.so', b'native')], link)
        with self.assertRaises(ValueError):
            self.restore()

    def test_duplicate_native_set_and_size_rejected(self):
        for files in ([('native/cgo/libmo.so', b'native')]*2,
                      [('gocache/aa/item', b'only-go')],
                      [('native/cgo/libmo.so', b'native'), ('native/cgo/extra.o', b'extra')]):
            self.pack(files)
            with self.assertRaises(ValueError):
                self.restore()
            self.assertFalse(self.cache.exists())

    def test_never_overwrite_existing_data_or_follow_symlinks(self):
        output = self.source/'cgo/libmo.so'
        output.write_text('existing')
        with self.assertRaises(ValueError):
            self.restore()
        self.assertEqual(output.read_text(), 'existing')
        output.unlink()  # This test fixture owns exactly this file.
        self.cache.symlink_to(self.root/'elsewhere')
        with self.assertRaises(ValueError):
            self.restore()

    def test_low_space_rejected(self):
        with patch.object(c.shutil, 'disk_usage', return_value=type('Space', (), {'free': 1})()):
            with self.assertRaisesRegex(ValueError, 'space'):
                c.restore(self.source, self.cache, self.artifact, self.manifest)

    def test_scope_flags_order_and_all_packages_match(self):
        selected, full, cover = (self.root/n for n in ('selected','full','cover'))
        selected.write_text('p/a\n'); full.write_text('p/a\np/b\n'); cover.write_text('p/a\n')
        c.verify_scope(self.manifest, selected, full, cover)
        for key in ('packages','full_packages','coverpkg','flags'):
            m = copy.deepcopy(self.manifest); m['coverage_contract'][key].append('different')
            with self.assertRaises(ValueError):
                c.verify_scope(m, selected, full, cover)

    def test_catalog_exact_version_env_group_no_latest_fallback(self):
        entry = {'source_repository':'org/repo','source':'a'*40,'environment':'mnl2','group':'1'}
        catalog = {'schema':1,'entries':[entry]}
        self.assertEqual(c.select_entry(catalog,'org/repo','a'*40,'mnl2','1'), entry)
        for args in [('fork/repo','a'*40,'mnl2','1'), ('org/repo','b'*40,'mnl2','1'),
                     ('org/repo','a'*40,'mty','1'), ('org/repo','a'*40,'mnl2','0')]:
            self.assertIsNone(c.select_entry(catalog,*args))
        catalog['entries'].append(entry)
        with self.assertRaises(ValueError):
            c.select_entry(catalog,'org/repo','a'*40,'mnl2','1')

    def reviewed(self):
        catalog = json.loads((Path(__file__).parents[1]/'scripts/fixtures/reviewed-cache-catalog.json').read_text())
        e = catalog['entries'][0]
        p = e['producer']
        run = {**p, 'repository': {'full_name':p['repository']}, 'actor': {'login':p['actor']},
               'triggering_actor': {'login':p['actor']}, 'status':'completed','conclusion':'success'}
        artifact = {**e['artifact'],'expired':False,'workflow_run':{'id':p['id'],'head_sha':p['head_sha']}}
        return e, run, artifact

    def test_provenance_full_identity_not_just_success(self):
        entry, run, artifact = self.reviewed()
        c.validate_provenance(entry, run, artifact)
        for field in ('id','head_sha','head_branch','event','path','workflow_id','run_attempt','conclusion'):
            modified = copy.deepcopy(run); modified[field] = 'different'
            with self.subTest(field=field), self.assertRaises(ValueError):
                c.validate_provenance(entry, modified, artifact)
        for field in ('actor','triggering_actor','repository','referenced_workflows'):
            modified = copy.deepcopy(run)
            modified[field] = [] if field == 'referenced_workflows' else {'login':'different','full_name':'different'}
            with self.subTest(field=field), self.assertRaises(ValueError):
                c.validate_provenance(entry, modified, artifact)
        for field in ('id','name','size_in_bytes','digest'):
            modified = copy.deepcopy(artifact); modified[field] = 'different'
            with self.subTest(field=field), self.assertRaises(ValueError):
                c.validate_provenance(entry, run, modified)

    def test_select_hit_miss_expiry_and_read_unavailability(self):
        entry, run, artifact = self.reviewed()
        catalog = self.root/'catalog.json'; catalog.write_text(json.dumps({'schema':1,'entries':[entry]}))
        state, output = self.root/'state.json', self.root/'output'
        argv = ['cache','select','--catalog',str(catalog),'--state',str(state)]
        env = {'GITHUB_WORKSPACE':str(self.source),'RUNNER_NAME':'cs-mnl2-large-test',
               'CS_CACHE_SOURCE_REPOSITORY':'matrixorigin/matrixone','COVERAGE_GROUP':'0',
               'GOCACHE':str(self.cache),'GITHUB_OUTPUT':str(output), 'CS_CACHE_READ_TOKEN':'never-print-this-value'}
        def invoke(responses, current=None):
            stream = io.StringIO()
            with patch.dict(os.environ,env), patch.object(sys,'argv',argv), \
                 patch.object(c.subprocess,'check_output',return_value=entry['source']), \
                 patch.object(c,'fingerprint',return_value=current or c.compatibility(entry['fingerprint'])), \
                 patch.object(c.shutil,'disk_usage',return_value=type('Space',(),{'free':80*c.GIB})()), \
                 patch.object(c,'api',side_effect=responses), contextlib.redirect_stdout(stream):
                c.main()
            self.assertNotIn('never-print-this-value',stream.getvalue())
            return stream.getvalue()
        self.assertIn('expired',invoke([run,{**artifact,'expired':True}]))
        self.assertFalse(state.exists())
        self.assertIn('unavailable',invoke(PermissionError()))
        self.assertIn('mismatch',invoke([],{'different':'platform'}))
        self.assertFalse(state.exists())
        invoke([run,artifact])
        self.assertTrue(state.exists()); self.assertIn('candidate=true',output.read_text())

    def test_manifest_review_hash_and_fingerprint_are_required(self):
        self.manifest['fingerprint'] = {'source':'a'*40}
        path = self.artifact/'manifest.json'; path.write_text(json.dumps(self.manifest))
        entry = {'fingerprint':self.manifest['fingerprint'],'manifest_sha256':c.sha256(path)}
        c.load_manifest(entry,self.artifact,{'source':'a'*40})
        with self.assertRaises(ValueError):
            c.load_manifest(entry,self.artifact,{'source':'b'*40})
        path.write_text(path.read_text()+' ')
        with self.assertRaises(ValueError):
            c.load_manifest(entry,self.artifact,{'source':'a'*40})


class WorkflowContract(unittest.TestCase):
    def test_read_only_restore_and_cold_miss_keep_test_contract(self):
        text = (Path(__file__).parents[1]/'.github/workflows/coverage-mty-pvc-consumer.yaml').read_text()
        for marker in ('max-parallel: 3','fail-fast: false','cs_coverage_cache.py verify-native',
                       'cs_coverage_cache.py verify-scope','make config; make cgo', '-count=1',
                       'CS_COVERAGE_CACHE_READ_TOKEN || github.token', 'cache: false',
                       'No automatic seed creation'.lower()):
            self.assertIn(marker, text)
        self.assertNotIn('cs_coverage_cache.py seed',text)
        self.assertNotIn('secrets.TOKEN_ACTION || github.token',text)
        block = text.split('- id: restore_reviewed_cache',1)[1].split('- name: Record reviewed cache outcome',1)[0]
        self.assertNotIn('continue-on-error',block)
        self.assertIn("steps.download_reviewed_cache.outcome == 'success'",block)


if __name__ == '__main__':
    unittest.main()
