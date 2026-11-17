import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import incremental_cache as c


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.config={'schema':2,'local':str(self.root/'job'),'shared':str(self.root/'shared'),
                     'indexes':[],'runtime':{'environment':'fixture','go':'fixture'},'reserve_bytes':0}

    def publish_fixture(self, cache):
        out=self.root/'candidate.json'
        index=c.export_index(cache.local,self.config['runtime'],out)
        shared=Path(self.config['shared'])
        (shared/'indexes').mkdir(parents=True)
        shutil.copyfile(out,shared/'indexes'/(index+'.json'))
        shutil.copytree(cache.local/'objects',shared/'objects')
        return index

    def test_private_put_shared_get_and_private_paths(self):
        cache=c.Cache(self.config)
        data=b'compiled object';action='a'*64;output=hashlib.sha256(data).hexdigest()
        self.assertTrue(cache.get(action)['Miss'])
        cache.put(action,output,len(data),io.BytesIO(data))
        index=self.publish_fixture(cache)
        reader=c.Cache({**self.config,'local':str(self.root/'reader'),'indexes':[index]})
        hit=reader.get(action)
        self.assertEqual(Path(hit['DiskPath']).read_bytes(),data)
        self.assertIn(self.root/'reader',Path(hit['DiskPath']).parents)
        self.assertNotIn(Path(self.config['shared']),Path(hit['DiskPath']).parents)
        self.assertEqual(reader.stats['shared_hits'],1)

    def test_corrupt_object_and_index_rejected(self):
        cache=c.Cache(self.config);data=b'object';output=hashlib.sha256(data).hexdigest()
        cache.put('a'*64,output,len(data),io.BytesIO(data))
        index=self.publish_fixture(cache)
        cfg={**self.config,'local':str(self.root/'reader'),'indexes':[index]}
        obj=Path(self.config['shared'])/'objects'/output[:2]/output
        obj.chmod(0o600)
        obj.write_bytes(b'broken')
        with self.assertRaises(ValueError):c.Cache(cfg).get('a'*64)
        path=Path(self.config['shared'])/'indexes'/(index+'.json')
        path.write_bytes(b'{}')
        with self.assertRaises(ValueError):c.Cache(cfg)

    def test_reject_overlap_and_symlink(self):
        with self.assertRaises(ValueError):c.Cache({**self.config,'local':str(self.root/'shared'/'job')})
        target=self.root/'target';target.mkdir()
        (self.root/'shared').symlink_to(target)
        with self.assertRaises(ValueError):c.Cache(self.config)

    def test_collision_and_wrong_hash_rejected(self):
        cache=c.Cache(self.config);data=b'first';output=hashlib.sha256(data).hexdigest()
        with self.assertRaises(ValueError):cache.put('a'*64,'b'*64,len(data),io.BytesIO(data))
        cache.put('a'*64,output,len(data),io.BytesIO(data))
        different=b'next'
        with self.assertRaises(ValueError):cache.put('a'*64,hashlib.sha256(different).hexdigest(),len(different),io.BytesIO(different))

    def test_protocol_roundtrip_and_failed_marker(self):
        data=b'bytes';action=base64.b64encode(b'a'*32).decode();output=base64.b64encode(hashlib.sha256(data).digest()).decode()
        request={'ID':1,'Command':'put','ActionID':action,'OutputID':output,'BodySize':len(data)}
        raw=c.canonical(request)+b'\n'+c.canonical(base64.b64encode(data).decode())+b'\n'
        raw+=c.canonical({'ID':2,'Command':'get','ActionID':action})+b'\n'
        raw+=c.canonical({'ID':3,'Command':'close'})+b'\n'
        out=io.BytesIO();c.protocol(self.config,io.BytesIO(raw),out)
        responses=[json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(responses[2]['OutputID'],output)
        bad=c.canonical({'ID':4,'Command':'get','ActionID':'bad'})+b'\n'
        with self.assertRaises(Exception):c.protocol(self.config,io.BytesIO(bad),io.BytesIO())
        self.assertTrue((Path(self.config['local'])/'validation-failed.json').is_file())
        with self.assertRaises(ValueError):c.export_index(Path(self.config['local']),{},self.root/'no-export.json')

    def test_multiple_process_equivalent_writers_are_atomic(self):
        data=b'concurrent';output=hashlib.sha256(data).hexdigest()
        def write(_):return c.Cache(self.config).put('a'*64,output,len(data),io.BytesIO(data))
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(write,range(16)))
        self.assertEqual(Path(c.Cache(self.config).get('a'*64)['DiskPath']).read_bytes(),data)


class RealGoIncrementalTests(unittest.TestCase):
    def test_compile_only_batches_reuse_actions_without_executing_tests(self):
        import node_cache_candidate as candidate
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix='go-batch-proof-') as tmp:
            root=Path(tmp).resolve();source=root/'source'
            shutil.copytree(Path(__file__).parent/'fixtures/incremental-go',source)
            config=root/'config.json'
            config.write_bytes(c.canonical({'schema':2,'local':str(root/'private'),
                'shared':str(root/'shared'),'indexes':[],'runtime':{'environment':'fixture'},'reserve_bytes':0}))
            env={k:v for k,v in os.environ.items() if k not in ('GOROOT','GOFLAGS','GOCACHEPROG','GOTOOLCHAIN')}
            env.update(GOTOOLCHAIN='local',GOMAXPROCS='2',GOWORK='off',GOPROXY='off',GOSUMDB='off',
                TEST_MARKER=str(root/'test-executions'),EXPECTED_VALUE='wrong-if-tests-execute',
                GOCACHE=str(root/'fallback'),GOCACHEPROG=f'{sys.executable} {Path(c.__file__).resolve()} --config {config}')
            packages=['example.test/cachefixture/'+x for x in ('stable','changed','app')]
            contract={'packages':packages,'coverpkg':packages,'flags':['-p','2','-count=1','-x','-covermode=set']}
            def run(commands):
                logs=''
                for command in commands:
                    proc=subprocess.run(command,cwd=source,env=env,capture_output=True,text=True,timeout=300)
                    self.assertEqual(proc.returncode,0,proc.stderr[-3000:]);logs+=proc.stderr
                self.assertFalse((root/'private/validation-failed.json').exists())
                self.assertFalse((root/'test-executions').exists())
                return logs
            # Initial single invocation and then three bounded invocations:
            # unchanged packages must retain the same compile action IDs.
            run(candidate.compile_commands(contract)[1:])
            with patch.object(candidate,'COVERAGE_BATCH_PACKAGES',1):
                logs=run(candidate.compile_commands(contract)[1:])
            self.assertFalse(any('/compile ' in line and '-p example.test/cachefixture/' in line
                                 for line in logs.splitlines()),logs[-3000:])

    def test_real_disk_cache_migrates_without_recompiling(self):
        import publish_node_cache as publisher
        with tempfile.TemporaryDirectory(prefix='go-cache-migration-') as tmp:
            root=Path(tmp).resolve();source=root/'source'
            shutil.copytree(Path(__file__).parent/'fixtures/incremental-go',source)
            env={k:v for k,v in os.environ.items() if k not in ('GOROOT','GOFLAGS','GOCACHEPROG','GOTOOLCHAIN')}
            env.update(GOTOOLCHAIN='local',GOWORK='off',GOPROXY='off',GOSUMDB='off',GOCACHE=str(root/'disk'))
            subprocess.run(['go','build','./stable'],cwd=source,env=env,check=True,timeout=60)
            local=root/'converted';candidate=root/'candidate.json';runtime={'environment':'fixture','go':'local'}
            index=publisher.import_reviewed_disk_cache(root/'disk',local,runtime,candidate)
            shared=root/'shared';(shared/'indexes').mkdir(parents=True)
            shutil.copyfile(candidate,shared/'indexes'/(index+'.json'));shutil.copytree(local/'objects',shared/'objects')
            config=root/'config.json';config.write_bytes(c.canonical({'schema':2,'local':str(root/'new-runner'),
                'shared':str(shared),'indexes':[index],'runtime':runtime,'reserve_bytes':0}))
            env.update(GOCACHE=str(root/'fallback'),GOCACHEPROG=f'{sys.executable} {Path(c.__file__).resolve()} --config {config}')
            result=subprocess.run(['go','build','-x','./stable'],cwd=source,env=env,text=True,capture_output=True,timeout=60)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertFalse(any('/compile ' in line and '-p example.test/cachefixture/stable ' in line for line in result.stderr.splitlines()))
            stats=[json.loads(p.read_text()) for p in (root/'new-runner/stats').glob('*.json')]
            self.assertGreater(sum(s['shared_hits'] for s in stats),0)

    def test_new_runner_new_sha_and_changed_partition(self):
        with tempfile.TemporaryDirectory(prefix='go-incremental-proof-') as tmp:
            root=Path(tmp).resolve();source=root/'source'
            shutil.copytree(Path(__file__).parent/'fixtures/incremental-go',source)
            subprocess.run(['git','init','-q',str(source)],check=True)
            def commit(message):
                subprocess.run(['git','add','.'],cwd=source,check=True)
                subprocess.run(['git','-c','user.name=fixture','-c','user.email=fixture@example.test','commit','-qm',message],cwd=source,check=True)
                return subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()
            first_sha=commit('first')
            runtime={'environment':'fixture','toolchain':'real-local-go'}
            shared=root/'shared';first=root/'runner-a'
            env={k:v for k,v in os.environ.items() if k not in ('GOROOT','GOFLAGS','GOCACHEPROG','GOTOOLCHAIN')}
            env.update(GOTOOLCHAIN='local',GOMAXPROCS='2',GOWORK='off',GOPROXY='off',GOSUMDB='off',TEST_MARKER=str(root/'test-executions'),EXPECTED_VALUE='18')
            program=Path(c.__file__).resolve()
            def run(local,indexes,packages,expected='18'):
                local.mkdir(exist_ok=True)
                cfg={'schema':2,'local':str(local/'cache'),'shared':str(shared),'indexes':indexes,'runtime':runtime,'reserve_bytes':0}
                config=local/'config.json';config.write_bytes(c.canonical(cfg))
                e={**env,'GOCACHE':str(local/'fallback'),'GOCACHEPROG':f'{sys.executable} {program} --config {config}','EXPECTED_VALUE':expected}
                p=subprocess.run(['go','test','-p','2','-count=1','-x','-covermode=set',
                    '-coverpkg=example.test/cachefixture/stable,example.test/cachefixture/changed,example.test/cachefixture/app',
                    '-coverprofile='+str(local/'coverage.out'),*packages],cwd=source,env=e,capture_output=True,text=True,timeout=300)
                self.assertEqual(p.returncode,0,p.stderr[-5000:])
                self.assertFalse((local/'cache/validation-failed.json').exists())
                return p.stderr
            run(first,[],['./stable','./changed','./app'])
            candidate=root/'candidate.json'
            index=c.export_index(first/'cache',runtime,candidate)
            (shared/'indexes').mkdir(parents=True)
            shutil.copyfile(candidate,shared/'indexes'/(index+'.json'))
            shutil.copytree(first/'cache/objects',shared/'objects')
            shared_before={str(p.relative_to(shared)):c.digest(p) for p in shared.rglob('*') if p.is_file()}
            # A new SHA changes one package, not the stable dependency. Separate
            # invocations model a different partition; each has a fresh runner.
            changed=source/'changed/changed.go'
            changed.write_text(changed.read_text().replace('return 7','return 9'))
            self.assertNotEqual(first_sha,commit('change one package'))
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(run,root/'runner-b',[index],['./stable','./changed'],'20')
                b=pool.submit(run,root/'runner-c',[index],['./app'],'20')
                logs=a.result()+b.result()
            compile_lines=[line for line in logs.splitlines() if '/compile ' in line]
            self.assertFalse(any('-p example.test/cachefixture/stable ' in line for line in compile_lines),logs[-3000:])
            self.assertTrue(any('-p example.test/cachefixture/changed ' in line for line in compile_lines))
            self.assertTrue(any('-p example.test/cachefixture/app ' in line for line in compile_lines))
            # count=1 actually executes the test again; no cached success replay.
            self.assertEqual((root/'test-executions').read_text().splitlines(),['executed','executed'])
            self.assertEqual(shared_before,{str(p.relative_to(shared)):c.digest(p) for p in shared.rglob('*') if p.is_file()})
            for runner in ('runner-b','runner-c'):
                stats=[json.loads(p.read_text()) for p in (root/runner/'cache/stats').glob('*.json')]
                self.assertGreater(sum(s['shared_hits'] for s in stats),0)

    def test_external_c_library_change_invalidates_cgo_not_pure_go(self):
        with tempfile.TemporaryDirectory(prefix='go-native-proof-') as tmp:
            root=Path(tmp).resolve();source=root/'source'
            shutil.copytree(Path(__file__).parent/'fixtures/incremental-go',source)
            native=source/'cgopkg/native'
            env={k:v for k,v in os.environ.items() if k not in ('GOROOT','GOFLAGS','GOCACHEPROG','GOTOOLCHAIN')}
            env.update(GOTOOLCHAIN='local',GOMAXPROCS='2',GOWORK='off',GOPROXY='off',GOSUMDB='off',CGO_ENABLED='1')
            shared=root/'shared';runtime={'environment':'fixture','go':'local'}
            def build_native(value):
                (native/'external.c').write_text(f'int external_value(void) {{ return {value}; }}\n')
                subprocess.run(['cc','-c','external.c','-o','external.o'],cwd=native,check=True)
                subprocess.run(['ar','rcs','libexternal.a','external.o'],cwd=native,check=True)
                return c.digest(native/'libexternal.a')
            def run(name,indexes,value,libhash):
                local=root/name;local.mkdir()
                config=local/'config.json'
                config.write_bytes(c.canonical({'schema':2,'local':str(local/'cache'),'shared':str(shared),
                    'indexes':indexes,'runtime':runtime,'reserve_bytes':0}))
                e={**env,'GOCACHE':str(local/'fallback'),'GOCACHEPROG':f'{sys.executable} {Path(c.__file__).resolve()} --config {config}',
                   'CGO_CFLAGS':f'-DMO_NATIVE_CACHE_{libhash}=1','EXPECTED_NATIVE':str(value)}
                result=subprocess.run(['go','test','-p','2','-count=1','-x','./stable','./cgopkg'],cwd=source,env=e,capture_output=True,text=True,timeout=300)
                self.assertEqual(result.returncode,0,result.stderr[-5000:])
                return local,result.stderr
            first,_=run('first',[],7,build_native(7))
            candidate=root/'candidate.json';index=c.export_index(first/'cache',runtime,candidate)
            (shared/'indexes').mkdir(parents=True)
            shutil.copyfile(candidate,shared/'indexes'/(index+'.json'));shutil.copytree(first/'cache/objects',shared/'objects')
            second,logs=run('second',[index],9,build_native(9))
            lines=[line for line in logs.splitlines() if '/compile ' in line]
            self.assertFalse(any('-p example.test/cachefixture/stable ' in line for line in lines))
            self.assertTrue(any('-p example.test/cachefixture/cgopkg ' in line for line in lines))
            self.assertFalse((second/'cache/validation-failed.json').exists())


if __name__=='__main__':unittest.main()
