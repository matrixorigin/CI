import concurrent.futures
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
import warnings
import zipfile

from scripts import cache_refresh_demand as d
from scripts import cache_refresh_prepare as builder


class Demand(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.db=sqlite3.connect(self.path/'ledger')
        self.addCleanup(self.db.close)
        self.p=dict(site='jed',node='mo-standard-1',node_uid='1'*8+'-1111-1111-1111-'+'1'*12,
                    pvc_uid='2'*8+'-2222-2222-2222-'+'2'*12,pv_uid='3'*8+'-3333-3333-3333-'+'3'*12,
                    runtime='a'*64,native='b'*64,modules='c'*64,recipe=d.RECIPE)
        self.h=dict(schema=1,profile=self.p,head='e'*40,runtime_missing=True,native_missing=True)
        self.a=dict(shared_hits=0,go_compiler_invocations=1170,native_hit=False)
        self.t=dict(profile=self.p,upstream_commit='d'*40,admitted=True,runtime_missing=True,
                    native_missing=True,module_profile_missing=False)
        self.calls=[]

    def backend(self,op,req):
        self.calls.append(op)
        if op=='inspect':
            return self.t
        self.assertNotIn('head',req)
        return dict(operation=op,key=req['key'],upstream_commit=self.t['upstream_commit'],
                    index_sha256='a'*64,success=True)

    def test_cross_group_claim_once_after_restart(self):
        def one(group):
            with closing(sqlite3.connect(self.path/'ledger')) as db:
                return d.claim(db,{**self.h,'group':group,'head':str(group)*40},self.a,self.t)['decision']
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            self.assertCountEqual(ex.map(one,[0,1]),['claimed','duplicate'])
        self.assertEqual(one(1),'duplicate')

    def test_node_keys_independent(self):
        a=d.claim(self.db,self.h,self.a,self.t)
        p={**self.p,'pvc_uid':'4'*8+'-4444-4444-4444-'+'4'*12}
        b=d.claim(self.db,{**self.h,'profile':p},self.a,{**self.t,'profile':p})
        self.assertNotEqual(a['key'],b['key'])

    def test_no_action_for_unmerged_fork_drift(self):
        self.t['profile']={**self.p,'modules':'f'*64}
        self.assertEqual(d.reconcile(self.db,self.h,self.a,self.backend)['decision'],
                         'waiting-for-compatible-trusted-source')
        self.assertEqual(self.calls,['inspect'])

    def test_warm_partial_compile_does_not_build(self):
        self.t.update(runtime_missing=False,native_missing=False)
        self.assertEqual(d.plan(self.h,{**self.a,'shared_hits':7145},self.t),'already-covered')

    def test_no_action_for_failed_admission(self):
        self.t['admitted']=False
        self.assertEqual(d.reconcile(self.db,self.h,self.a,self.backend)['decision'],'hold')
        self.assertEqual(self.calls,['inspect'])

    def test_module_change_after_trusted_source_matches(self):
        self.t.update(runtime_missing=False,native_missing=False,module_profile_missing=True)
        self.assertEqual(d.plan(self.h,{**self.a,'shared_hits':310,'native_hit':True},self.t),'prepare')

    def test_use_protected_build_not_pr_payload(self):
        self.t['trusted_candidate_index']='a'*64
        self.h['trusted_candidate_index']='z'*64
        r=d.reconcile(self.db,self.h,self.a,self.backend)
        self.assertEqual(r['decision'],'activated')
        self.assertEqual(self.calls,['inspect','verify_candidate','append_missing','readback','activate'])

    def test_prepare_then_verify_then_publish(self):
        d.reconcile(self.db,self.h,self.a,self.backend)
        self.assertEqual(self.calls,['inspect','prepare','verify_candidate','append_missing','readback','activate'])

    def test_builder_takes_full_scope_not_triggering_group(self):
        pkg='github.com/matrixorigin/matrixone/pkg/'
        self.assertEqual(builder.full_scope('\n'.join(pkg+n for n in ('one','catalog','two'))),
                         pkg+'one\n'+pkg+'two\n')
        with self.assertRaises(ValueError):
            builder.full_scope(pkg+'one\n'+pkg+'one')

    def test_builder_rejects_fork_remote_or_untracked_file(self):
        def git(*args):
            return subprocess.check_output(['git','-c','user.name=Test','-c','user.email=t@example.invalid',
                                            *args],cwd=self.path,text=True).strip()
        git('init','-q'); git('commit','--allow-empty','-qm','fixture')
        head=git('rev-parse','HEAD')
        git('remote','add','origin','https://github.com/fork/matrixone.git')
        with self.assertRaisesRegex(ValueError,'unexpected upstream'):
            builder.checkout(self.path,'matrixone',head)
        git('remote','set-url','origin','https://github.com/matrixorigin/matrixone.git')
        # This test's ledger is also untracked and must not pass the pristine check.
        with self.assertRaisesRegex(ValueError,'pristine'):
            builder.checkout(self.path,'matrixone',head)

    def test_readback_mismatch_never_activates_or_republishes(self):
        def wrong(op,req):
            r=self.backend(op,req)
            if op=='readback':
                r['index_sha256']='f'*64
            return r
        with self.assertRaises(ValueError):
            d.reconcile(self.db,self.h,self.a,wrong)
        self.assertNotIn('activate',self.calls)
        self.assertEqual(self.db.execute('select phase from intents').fetchone()[0],'readback:intent')
        self.assertEqual(d.reconcile(self.db,self.h,self.a,self.backend)['decision'],'duplicate')

    def test_report_allowlist(self):
        p=self.path/'report.zip'
        with zipfile.ZipFile(p,'w') as z:
            z.writestr('ignored','payload')
        self.assertIsNone(d.report_inputs(p))
        with zipfile.ZipFile(p,'a') as z:
            for name,obj in [('cache-refresh-hint.json',self.h),('cs-node-cache/acceptance.json',self.a),
                             ('cs-node-cache/native-result.json',self.a),('go-rebuild-counts.json',self.a)]:
                z.writestr(name,json.dumps(obj))
        self.assertEqual(d.report_inputs(p),(self.h,self.a))
        with warnings.catch_warnings(),zipfile.ZipFile(p,'a') as z:
            warnings.simplefilter('ignore',UserWarning)
            z.writestr('cache-refresh-hint.json','{}')
        with self.assertRaises(ValueError): d.report_inputs(p)

    def test_observer_requires_verified_volume_and_real_runtime_gap(self):
        repo=self.path/'repo'; repo.mkdir()
        def git(*cmd):
            return subprocess.check_output(['git','-c','user.name=Test','-c','user.email=test@example.invalid',
                                            *cmd],cwd=repo,text=True).strip()
        git('init','-q'); (repo/'go.mod').write_text('module test'); (repo/'go.sum').write_text('')
        git('add','go.mod','go.sum');git('commit','-qm','fixture')
        head=git('rev-parse','HEAD')
        b={k:self.p[k] for k in ('node','pv_uid','pvc_uid','node_uid')}; b['environment']='jed'
        runtime={'environment':'jed','repository':'matrixorigin/matrixone'}
        data={'cs-node-cache/state.json':{'config':{'runtime':runtime,'indexes':[],
              'shared':'/ci-cache/incremental-v2'},'native_key':'b'*64},
              'cs-node-cache/pr-source.json':{'checked_out_head_sha':head,'actual_pr_head_sha':head,'shared_cache_writes':0},
              'cs-node-cache/persistent-authorization.json':{'authorization_id':'test','revocations_sha256':'test','authorized_indexes':[]},
              'cs-reviewed-persistent-catalog.json':{'nodes':[b],'entries':[
                  {'bindings':[{k:b[k] for k in ('pv_uid','pvc_uid','node_uid')}],'runtime':{'old':True},'index_sha256':'c'*64}]},
              'persistent-toolchain.json':{'persistent_tools':True,'binding':b}}
        (self.path/'cs-node-cache').mkdir()
        for n,obj in data.items(): (self.path/n).write_text(json.dumps(obj))
        h=d.observe(self.path,repo)
        self.assertTrue(h['runtime_missing'])
        self.assertEqual(h['profile']['modules'],d.modules(repo,head))
        cat=data['cs-reviewed-persistent-catalog.json'];cat['entries'][0]['runtime']=runtime
        (self.path/'cs-reviewed-persistent-catalog.json').write_text(json.dumps(cat))
        with self.assertRaisesRegex(ValueError,'unselected or revoked'): d.observe(self.path,repo)
