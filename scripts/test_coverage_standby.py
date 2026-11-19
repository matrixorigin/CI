import unittest
import json
import os
from unittest.mock import patch
from scripts import coverage_standby as s


class ParentAdmission(unittest.TestCase):
    def setUp(self):
        self.sha = '3b95c0878f88b3da58bf3e65c369b7ef4d13104e'
        self.src = 'Ariznawlll/matrixone'
        self.t = dict(parent=42, attempt=1, group=0, pr=32922, source=self.src, head=self.sha,
                      key=s.digest('test-nonce'), revision='b'*40)
        self.p = dict(id=42, run_attempt=1, repository={'full_name':s.REPO}, path=s.ENTRY,
                      event='pull_request_target', head_repository={'full_name':self.src},
                      head_sha=self.sha, status='in_progress', conclusion=None)

    def test_only_active_parent_returns(self):
        with patch.object(s,'run',side_effect=[dict(self.p,status=v) for v in
                          ('queued','waiting','in_progress')]) as read, patch.object(s.time,'sleep') as sleep:
            self.assertEqual(s.parent_meta(self.t)['id'],42)
            self.assertEqual((read.call_count,sleep.call_count),(3,2))

    def test_queued_parent_revalidated_each_time(self):
        for field,bad in [('id',43),('run_attempt',2),('repository',{'full_name':'fork/other'}),
                          ('path','wrong-workflow'),('event','workflow_dispatch'),
                          ('head_repository',None),('head_sha','c'*40)]:
            with self.subTest(field=field), patch.object(s,'run',side_effect=[
                    dict(self.p,status='queued'),{**self.p,field:bad}]), patch.object(s.time,'sleep') as sleep:
                with self.assertRaisesRegex(ValueError,'identity mismatch: '+field):
                    s.parent_meta(self.t)
                self.assertEqual(sleep.call_count,1)

    def test_terminal_unknown_and_incomplete_states_fail_closed(self):
        for p in (dict(self.p,status='completed'),dict(self.p,conclusion='cancelled'),
                  dict(self.p,status='unexpected'),{k:v for k,v in self.p.items() if k!='conclusion'}):
            with self.subTest(p=p), patch.object(s,'run',return_value=p), patch.object(s.time,'sleep') as sleep:
                with self.assertRaisesRegex(ValueError,'not active'):
                    s.parent_meta(self.t)
                sleep.assert_not_called()

    def test_queued_is_bounded_and_never_grants(self):
        with patch.object(s,'run',return_value=dict(self.p,status='queued')) as read, \
             patch.object(s.time,'sleep') as sleep, patch.object(s,'artifact') as artifact:
            with self.assertRaisesRegex(ValueError,'still not active'):
                s.parent_meta(self.t)
            self.assertEqual((read.call_count,sleep.call_count),(s.PARENT_POLLS,s.PARENT_POLLS-1))
            artifact.assert_not_called()

    def test_real_grant_path_and_terminal_change_before_checkout(self):
        grant=dict(child=51,ticket=self.t,pool='cs',nonce='test-nonce')
        proof=dict(allowed='true',verified_membership_http_status=204,pr_number='32922',
                   author='dev',head_repository=self.src,head_sha=self.sha)
        pr=dict(number=32922,user={'login':'dev'},head={'repo':{'full_name':self.src,'archived':False},
                'sha':self.sha},state='open',draft=False,base={'repo':{'full_name':s.REPO},'ref':'main'},
                labels=[{'name':'kind/test-ci'}])
        env=dict(TICKET=json.dumps(self.t),GROUP='0',HEAD=self.sha,SOURCE=self.src,POOL='cs',
                 GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='51')
        for cancel in (False,True):
            reads=[dict(self.p,status='queued'),self.p,self.p,
                   dict(self.p,status='completed',conclusion='cancelled') if cancel else self.p]
            with self.subTest(cancel=cancel), patch.dict(os.environ,env), patch.object(s,'run',side_effect=reads), \
                 patch.object(s,'child_meta'), patch.object(s.time,'sleep'), patch.object(s,'one',return_value=pr), \
                 patch.object(s,'artifact',side_effect=[grant,proof]):
                if cancel:
                    with self.assertRaisesRegex(ValueError,'cancelled'):
                        s.admit()
                else:
                    s.admit()


class Queue(unittest.TestCase):
    def setUp(self):
        self.q = dict(status='queued', runner_name='', runner_id=0, id=2)
        self.active = dict(self.q, status='in_progress', runner_name='cs-jed-nc-n1-pr-v1-abc-runner-def', runner_id=123)
        self.env = dict(GITHUB_RUN_ATTEMPT='1', GITHUB_RUN_ID='42', GROUP='0', HEAD='a'*40, SOURCE='fork/matrixone', PR='32922')

    def test_deadline(self):
        self.assertEqual(s.decide(self.q, 50, None), (None, 50))
        self.assertEqual(s.decide(self.q, 649, 50), (None, 50))
        self.assertEqual(s.decide(self.q, 650, 50), ('idc', 50))
        self.assertEqual(s.decide(self.active, 670, 50), ('cs', 50))
        self.assertEqual(s.decide(dict(self.q, runner_id=2), 670, 50), (None, None))
        self.assertEqual(s.decide(dict(self.q, status='waiting'), 670, 50), (None, None))
        with self.assertRaises(ValueError):
            s.decide(dict(self.q, status='completed'), 670, 50)

    def scenario(self, observations, cancellation, count, selected):
        import tempfile, os, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {**self.env, 'RUNNER_TEMP': tmp}), \
             patch.object(s,'parent_meta',return_value={'head_sha':'a'*40,'head_repository':{'full_name':'fork/matrixone'}}), \
             patch.object(s,'one',return_value={'object':{'sha':'b'*40}}), \
             patch.object(s,'dispatch',side_effect=[111,222]) as launch, \
             patch.object(s,'observe',side_effect=observations), \
             patch.object(s,'child_meta',return_value={'status':'completed','conclusion':'cancelled'}), \
             patch.object(s,'api') as api, patch.object(s.time,'sleep'), patch.object(s,'output'):
            s.choose()
            self.assertEqual(launch.call_count, count)
            self.assertEqual(api.call_count, int(cancellation))
            if cancellation:
                api.assert_called_once_with('/actions/runs/111/cancel', {})
            grant=json.loads(Path(tmp,'grant.json').read_text())
            self.assertEqual((grant['pool'],grant['child']),selected)
            self.assertEqual(grant['ticket']['key'],s.digest(grant['nonce']))

    def test_only_the_still_queued_child_is_withdrawn(self):
        self.scenario([(self.q,1),(self.q,601),(self.q,602)],True,2,('idc',222))

    def test_assigned_during_final_read_is_never_cancelled(self):
        self.scenario([(self.q,1),(self.q,601),(self.active,602)],False,1,('cs',111))

    def test_immediate_assignment(self):
        self.scenario([(self.active,2)],False,1,('cs',111))

    def test_empty_dispatch_response_reconciles_on_same_worker_ref_without_second_post(self):
        t = dict(parent=1, attempt=1, group=1, head='a'*40, key='b'*64)
        with patch.object(s, 'WORKER_REF', 'staging'), patch.object(s, 'parent_meta'), \
             patch.object(s, 'child_meta'), patch.object(s, 'api', return_value=({},0)) as post, \
             patch.object(s, 'one', return_value={'workflow_runs':[{'id':123,'display_title':s.title(t,'cs')}]}) as get:
            self.assertEqual(s.dispatch(t,'cs','c'*40),123)
            self.assertEqual(post.call_count,1)
            self.assertEqual(post.call_args.args[1]['ref'],'staging')
            self.assertIn('branch=staging',get.call_args.args[0])
