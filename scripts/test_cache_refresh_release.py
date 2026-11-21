import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import cache_refresh_release as r, cache_refresh_demand as d


class Reader(unittest.TestCase):
    def setUp(self):
        self.runtime={'environment':'jed','repository':'matrixorigin/matrixone'}
        self.state={'config':{'runtime':self.runtime},'native_key':'b'*64}
        self.p=dict(site='jed',node='mo-standard-0',runtime=d.digest(self.runtime),native='b'*64,
                    modules='c'*64,recipe=d.RECIPE)
        self.p.update({k:'00000000-0000-0000-0000-00000000000'+str(i)
                       for i,k in enumerate(('node_uid','pv_uid','pvc_uid'))})
        self.row=dict(schema=1,profile=self.p,profile_key=d.digest(self.p),index_sha256='a'*64,
                      native_manifests={},upstream_commit='d'*40,manifest_sha256='e'*64,readback_verified=True)
        self.rev={'indexes':[],'environments':[],'authorization_ids':[]}

    def select(self):
        return r.select(self.p,self.state,self.row,self.rev,['base'],d.digest)

    def test_requires_exact_approval(self):
        self.assertEqual(self.select()['index_sha256'],'a'*64)
        for key,bad in [('profile_key','d'*64),('profile',{**self.p,'node':'mo-standard-1'}),
                        ('readback_verified',False),('native_manifests',{'c'*64:'f'*64})]:
            with self.subTest(key=key),patch.dict(self.row,{key:bad}),self.assertRaises(ValueError):
                self.select()
        for key,bad in [('indexes',['a'*64]),('environments',['jed']),('authorization_ids',['base']),
                        ('authorization_ids',[d.digest(self.row)])]:
            with self.subTest(key=key),patch.dict(self.rev,{key:bad}): self.assertIsNone(self.select())
        self.state['native_authorities']={'b'*64:None}
        self.row['native_manifests']={'b'*64:'f'*64}
        self.assertIsNone(self.select())

    @unittest.skipUnless(os.getenv('CACHE_REFRESH_HELPERS'),'requires pinned consumer helper checkout')
    def test_next_process_reads_released_object_not_private_hit(self):
        helpers=Path(os.environ['CACHE_REFRESH_HELPERS']).resolve()
        sys.path[:0]=[str(helpers/'scripts'),str(Path(r.__file__).parent)]
        import incremental_cache as c, node_compile_cache as n, node_cache_native_pvc_catalog as catalog
        with tempfile.TemporaryDirectory() as td:
            tmp=Path(td).resolve(); shared=tmp/'shared'; root=tmp/'projection'; root.mkdir()
            src=tmp/'repo'; src.mkdir()
            def git(*args):
                return subprocess.check_output(['git','-c','user.name=test','-c','user.email=t@example.invalid',
                                                *args],cwd=src,text=True).strip()
            git('init','-q'); (src/'go.mod').write_text('module example.test/m\n'); (src/'go.sum').touch()
            git('add','.');git('commit','-qm','source');self.p['modules']=d.modules(src,git('rev-parse','HEAD'))
            b={k:self.p[k] for k in ('node','node_uid','pvc_uid','pv_uid')};b['environment']='jed'
            c.atomic_json(shared/'node-cache-identity.json',b)
            b['marker_sha256']=c.digest(shared/'node-cache-identity.json')
            c.atomic_json(tmp/'cs-reviewed-persistent-catalog.json',dict(schema=2,nodes=[b],entries=[]))
            go_src=tmp/'demo.go'; go_src.write_text('package demo\nfunc Answer() int {return 42}\n')
            archive=tmp/'demo.a'
            subprocess.run(['go','tool','compile','-p','demo','-o',str(archive),str(go_src)],check=True)
            action='1'*64; body=archive.read_bytes(); obj=hashlib.sha256(body).hexdigest()
            path=shared/'objects'/obj[:2]/obj;path.parent.mkdir(parents=True);path.write_bytes(body)
            ix=dict(schema=2,runtime=self.runtime,actions={action:dict(output_id=obj,size=len(body))})
            idx=d.digest(ix);c.atomic_json(shared/'indexes'/(idx+'.json'),ix)
            self.row.update(profile_key=d.digest(self.p),index_sha256=idx)
            c.atomic_json(root/(d.digest(self.p)+'.json'),self.row)
            local=tmp/'cs-node-cache';cfg=dict(schema=2,local=str(local/'objects-private'),
                                           shared=str(shared),indexes=[],runtime=self.runtime,reserve_bytes=0)
            c.atomic_json(local/'state.json',dict(config=cfg,native_key=self.p['native'],source=str(src),entries=[]))
            c.atomic_json(local/'config.json',cfg)
            env={**os.environ,'CS_CACHE_ENV':'jed','CS_CACHE_NODE':b['node'],'RUNNER_TEMP':str(tmp),
                 'GITHUB_ENV':str(tmp/'env'),**{'CS_CACHE_'+k.upper():b[k] for k in ('node_uid','pvc_uid','pv_uid')}}
            with patch.dict(os.environ,env),patch.object(r,'ROOT',root),patch.object(r,'SHARED',shared), \
                 patch.object(Path,'is_mount',return_value=True),patch.object(n,'readonly_mount'), \
                 patch.object(catalog,'authority',return_value=SimpleNamespace(ident='base',revoked=self.rev)):
                self.assertEqual(r.install(tmp,src,helpers),'selected '+idx)
            query=dict(ID=1,Command='get',ActionID=base64.b64encode(bytes.fromhex(action)).decode())
            cmd=[sys.executable,'-I',r.__file__,'--helpers',str(helpers)]
            data='\n'.join(map(json.dumps,[query,{**query,'ID':2},{'ID':3,'Command':'close'}]))+'\n'
            proc=subprocess.run([*cmd,'--cacheprog'],env=env,input=data,capture_output=True,text=True,check=True)
            reads=list(map(json.loads,proc.stdout.splitlines()))
            self.assertEqual(Path(reads[1]['DiskPath']).read_bytes(),body)
            subprocess.run([*cmd,'--summarize'],env=env,check=True)
            report=c.read_json(local/'release-consumption.json')
            self.assertEqual((report['index_sha256'],report['released_shared_go_reads']),(idx,1))
